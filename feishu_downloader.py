import configparser, locale, os, re, subprocess, time
from concurrent.futures import as_completed, ThreadPoolExecutor

import requests
from tqdm import tqdm

locale.setlocale(locale.LC_CTYPE,"chinese")


# 读取配置文件
config = configparser.ConfigParser(interpolation=None)
config.read('config.ini', encoding='utf-8')
# 获取配置文件中的cookie
minutes_cookie = config.get('Cookies', 'minutes_cookie')
manager_cookie = config.get('Cookies', 'manager_cookie')
# 获取下载设置
space_name = int(config.get('下载设置', '所在空间'))
list_size = int(config.get('下载设置', '每次检查的妙记数量'))
check_interval = int(config.get('下载设置', '检查妙记的时间间隔（单位s，太短容易报错）'))
download_type = int(config.get('下载设置', '文件类型'))
subtitle_only = True if config.get('下载设置', '是否只下载字幕文件（是/否）')=='是' else False
usage_threshold = float(config.get('下载设置', '妙记额度删除阈值（GB，填写了manager_cookie才有效）'))
# 获取保存路径
save_path = config.get('下载设置', '保存路径（不填则默认为当前路径/data）')
if not save_path:
    save_path = './data'
# 获取字幕格式设置
subtitle_params = {'add_speaker': True if config.get('下载设置', '字幕是否包含说话人（是/否）')=='是' else False,
                   'add_timestamp': True if config.get('下载设置', '字幕是否包含时间戳（是/否）')=='是' else False,
                   'format': 3 if config.get('下载设置', '字幕格式（srt/txt）')=='srt' else 2
                   }
# 获取代理设置
use_proxy = config.get('代理设置', '是否使用代理（是/否）')
proxy_address = config.get('代理设置', '代理地址')
if use_proxy == '是':
    proxies = {
        'http': proxy_address,
        'https': proxy_address,
    }
else:
    proxies = None


class FeishuDownloader:
    def __init__(self, cookie):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36',
            'cookie': cookie,
            'bv-csrf-token': cookie[cookie.find('bv_csrf_token=') + len('bv_csrf_token='):cookie.find(';', cookie.find('bv_csrf_token='))],
            'referer': f'https://meetings.feishu.cn/minutes/me',
            'content-type': 'application/x-www-form-urlencoded'
        }
        if len(self.headers.get('bv-csrf-token')) != 36:
            raise Exception("minutes_cookie中不包含bv_csrf_token，请确保从请求`list?size=20&`中获取！")

        self.meeting_time_dict = {} # 会议文件名称和会议时间的对应关系
        self.subtitle_type = 'srt' if subtitle_params['format']==3 else 'txt'
        
    def get_minutes(self):
        """
        批量获取妙记信息
        """
        get_rec_url = f'https://meetings.feishu.cn/minutes/api/space/list?&size={list_size}&space_name={space_name}'
        resp = requests.get(url=get_rec_url, headers=self.headers, proxies=proxies)
        # 如果resp.json()['data']中没有list字段，则说明cookie失效
        if 'list' not in resp.json()['data']:
            raise Exception("minutes_cookie失效，请重新获取！")
        return list(reversed(resp.json()['data']['list'])) # 返回按时间正序排列的妙记信息（从旧到新）

    def check_minutes(self):
        """
        检查需要下载的妙记
        """
        
        # 从文件中读取已下载的妙记id
        downloaded_minutes = set()
        if os.path.exists('minutes.txt'):
            with open('minutes.txt', 'r') as f:
                downloaded_minutes = set(line.strip() for line in f)
        
        # 获取所有妙记
        all_minutes = self.get_minutes()

        # 过滤需要下载的妙记
        need_download_minutes = [
            minutes for minutes in all_minutes
            if minutes['object_token'] not in downloaded_minutes and
            (download_type == 2 or minutes['object_type'] == download_type)
        ]

        # 如果有需要下载的妙记则进行下载
        if need_download_minutes:
            self.download_minutes(need_download_minutes)
            # 将下载的妙记id写入记录
            with open('minutes.txt', 'a') as f:
                for minutes in need_download_minutes:
                    f.write(minutes['object_token']+'\n')
            print(f"成功下载了{len(need_download_minutes)}个妙记，等待{check_interval}s后再次检查...")

    def download_minutes(self, minutes_list):
        """
        使用aria2批量下载妙记
        """
        with ThreadPoolExecutor(max_workers=10) as executor:
            with open('links.temp', 'w', encoding='utf-8') as file:
                futures = [executor.submit(self.get_minutes_url, minutes) for minutes in minutes_list]
                for future in as_completed(futures):
                    video_url = future.result()[0]
                    file_name = future.result()[1]
                    video_name = file_name
                    file.write(f'{video_url}\n out={save_path}/{file_name}/{video_name}.mp4\n')

        if not subtitle_only:
            headers_option = ' '.join(f'--header="{k}: {v}"' for k, v in self.headers.items())
            proxy_cmd = ""
            if proxies is not None:
                proxy_cmd = f'--all-proxy={proxies["http"]}'
            cmd = f'aria2c -c --input-file=links.temp {headers_option} --continue=true --auto-file-renaming=true --console-log-level=warn {proxy_cmd} -s16 -x16 -k1M'
            subprocess.run(cmd, shell=True)

        # 删除临时文件
        os.remove('links.temp')

        # 修改会议妙记的创建时间
        for file_name, start_time in self.meeting_time_dict.items():
            os.utime(f'{save_path}/{file_name}', (start_time, start_time))
            if not subtitle_only:
                os.utime(f'{save_path}/{file_name}/{file_name}.mp4', (start_time, start_time))
            os.utime(f'{save_path}/{file_name}/{file_name}.{self.subtitle_type}', (start_time, start_time))
        self.meeting_time_dict = {}

    def get_minutes_url(self, minutes):
        """
        获取妙记视频下载链接；写入字幕文件。
        """
        # 获取妙记视频的下载链接
        video_url_url = f'https://meetings.feishu.cn/minutes/api/status?object_token={minutes["object_token"]}&language=zh_cn&_t={int(time.time() * 1000)}'
        resp = requests.get(url=video_url_url, headers=self.headers, proxies=proxies)
        video_url = resp.json()['data']['video_info']['video_download_url']

        # 获取妙记字幕
        subtitle_url = f'https://meetings.feishu.cn/minutes/api/export'
        subtitle_params['object_token'] = minutes['object_token']
        resp = requests.post(url=subtitle_url, params=subtitle_params, headers=self.headers, proxies=proxies)
        resp.encoding = 'utf-8'

        # 获取妙记标题
        file_name = minutes['topic']
        rstr = r'[\/\\\:\*\?\"\<\>\|]'
        file_name = re.sub(rstr, '_', file_name)  # 将标题中的特殊字符替换为下划线
        
        # 如果妙记来自会议，则将会议起止时间作为文件名的一部分
        if minutes['object_type'] == 0:
            # 根据会议的起止时间和标题来设置文件名
            start_time = time.strftime("%Y年%m月%d日%H时%M分", time.localtime(minutes['start_time'] / 1000))
            stop_time = time.strftime("%Y年%m月%d日%H时%M分", time.localtime(minutes['stop_time'] / 1000))
            file_name = start_time+"至"+stop_time+file_name
        else:
            create_time = time.strftime("%Y年%m月%d日%H时%M分", time.localtime(minutes['create_time'] / 1000))
            file_name = create_time+file_name
        
        subtitle_name = file_name
            
        # 创建文件夹
        if not os.path.exists(f'{save_path}/{file_name}'):
            os.makedirs(f'{save_path}/{file_name}')

        # 写入字幕文件
        with open(f'{save_path}/{file_name}/{subtitle_name}.{self.subtitle_type}', 'w', encoding='utf-8') as f:
            f.write(resp.text)
        
        # 如果妙记来自会议，则记录会议起止时间
        if minutes['object_type'] == 0:
            self.meeting_time_dict[file_name] = minutes['start_time']/1000

        return video_url, file_name

    def delete_minutes(self, num):
        """
        删除指定数量的最早几个妙记
        """
        all_minutes = self.get_minutes()

        for index in tqdm(all_minutes[:num], desc='删除妙记'):
            try:
                # 将该妙记放入回收站
                delete_url = f'https://meetings.feishu.cn/minutes/api/space/delete'
                params = {'object_tokens': index['object_token'],
                        'is_destroyed': 'false',
                        'language': 'zh_cn'}
                resp = requests.post(url=delete_url, params=params, headers=self.headers, proxies=proxies)
                if resp.status_code != 200:
                    raise Exception(f"删除妙记 http://meetings.feishu.cn/minutes/{index['object_token']} 失败！{resp.json()}")

                # 将该妙记彻底删除
                params['is_destroyed'] = 'true'
                resp = requests.post(url=delete_url, params=params, headers=self.headers, proxies=proxies)
                if resp.status_code != 200:
                    raise Exception(f"删除妙记 http://meetings.feishu.cn/minutes/{index['object_token']} 失败！{resp.json()}")
            except Exception as e:
                print(f"{e} 可能是没有该妙记的权限。")
                num += 1
                continue

if __name__ == '__main__':

    if not minutes_cookie:
        raise Exception("cookie不能为空！")
    
    # 如果未填写管理参数，则定时检查是否有要下载的妙记
    elif not manager_cookie:
        while True:
            print(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
            downloader = FeishuDownloader(minutes_cookie)
            # 如果下载到了妙记则删除最早的一个妙记
            if downloader.check_minutes():
                downloader.delete_minutes(1)
            time.sleep(check_interval)

    # 如果填写了管理参数，则定时查询妙记空间使用情况，超出指定额度则删除最早的指定数量的妙记
    else:
        # 从manager_cookie中获取X-Csrf-Token
        x_csrf_token = manager_cookie[manager_cookie.find(' csrf_token=') + len(' csrf_token='):manager_cookie.find(';', manager_cookie.find(' csrf_token='))]
        if len(x_csrf_token) != 36:
            raise Exception("manager_cookie中不包含csrf_token，请确保从请求`count?_t=`中获取！")

        usage_bytes_old = 0 # 上次记录的已经使用的字节数
        # 定期查询已使用的妙记空间字节数
        while True:
            print(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
            # 查询妙记空间已用字节数
            query_url = f'https://www.feishu.cn/suite/admin/api/gaea/usages'
            manager_headers = {'cookie': manager_cookie, 'X-Csrf-Token':x_csrf_token}
            res = requests.get(url=query_url, headers=manager_headers, proxies=proxies)
            usage_bytes = int(res.json()['data']['items'][6]['usage']) # 查询到的目前已用字节数
            print(f"已用空间：{usage_bytes / 2 ** 30:.2f}GB")
            # 如果已用字节数有变化则下载妙记
            if usage_bytes != usage_bytes_old:
                downloader = FeishuDownloader(minutes_cookie)
                downloader.check_minutes()
                # 如果已用超过9.65G则删除最早的两个妙记
                if usage_bytes > 2 ** 30 * usage_threshold:
                    downloader.delete_minutes(2)
            else:
                print(f"等待{check_interval}s后再次检查...")
            usage_bytes_old = usage_bytes #　更新已用字节数
            
            time.sleep(check_interval)

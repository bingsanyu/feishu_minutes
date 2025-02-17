import configparser, locale, os, re, subprocess, time
from concurrent.futures import as_completed, ThreadPoolExecutor

import requests
from tqdm import tqdm

locale.setlocale(locale.LC_CTYPE,"chinese")


# 读取配置文件
config = configparser.ConfigParser(interpolation=None)
config.read('config.ini', encoding='utf-8')
# 获取配置文件中的cookie
minutes_cookie = config.get('Cookies', 'cookie')
# 获取下载设置
space_name = int(config.get('下载设置', '所在空间'))
vc_max_num = int(config.get('下载设置', '保留云端妙记的最大数量'))
check_interval = int(config.get('下载设置', '检查妙记的时间间隔（单位s，太短容易报错）'))
download_type = int(config.get('下载设置', '文件类型'))
subtitle_only = config.get('下载设置', '是否只下载字幕文件（是/否）')=='是'
# 获取保存路径
save_path = config.get('下载设置', '保存路径（不填则默认为当前路径/data）')
if not save_path:
    save_path = './data'
# 获取字幕格式设置
subtitle_params = {'add_speaker': config.get('下载设置', '字幕是否包含说话人（是/否）')=='是',
                   'add_timestamp': config.get('下载设置', '字幕是否包含时间戳（是/否）')=='是',
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
        self.all_minutes = []
        self.minutes_num = 0
        self.meeting_time_dict = {} # 会议文件名称和会议时间的对应关系
        self.subtitle_type = 'srt' if subtitle_params['format']==3 else 'txt'
        
    def get_minutes(self, last_timestamp=None):
        """
        批量获取妙记信息
        Args:
            last_timestamp: 上一次请求的最后一个会议的时间戳
        """
        base_url = f'https://meetings.feishu.cn/minutes/api/space/list?size=20&space_name={space_name}'
        if last_timestamp:
            get_rec_url = f'{base_url}&timestamp={last_timestamp}'
        else:
            get_rec_url = base_url
            self.all_minutes = []
        resp = requests.get(url=get_rec_url, headers=self.headers, proxies=proxies)
        data = resp.json()['data']
        if 'list' not in data:
            raise Exception("minutes_cookie失效，请重新获取！")
        current_list = data['list']
        self.all_minutes.extend(current_list)
        if data.get('has_more', True) and current_list:
            # 获取最后一个会议的时间戳
            last_meeting = current_list[-1]
            next_timestamp = last_meeting.get('share_time')
            if next_timestamp:
                self.get_minutes(next_timestamp)
        # 所有数据获取完成后，对列表进行反转（从旧到新排序）
        if not last_timestamp:  # 只在最初的调用中执行
            self.all_minutes = list(reversed(self.all_minutes))
            self.minutes_num = len(self.all_minutes)

    def check_minutes(self):
        """
        检查需要下载的妙记
        """
        
        # 从文件中读取已下载的妙记id
        downloaded_minutes = set()
        if os.path.exists('minutes.txt'):
            with open('minutes.txt', 'r') as f:
                downloaded_minutes = set(line.strip() for line in f)
        
        # 获取云端所有妙记
        self.get_minutes()
        print(f"云端现有 {self.minutes_num} 个妙记")

        # 过滤需要下载的妙记
        need_download_minutes = [
            minutes for minutes in self.all_minutes
            if minutes['object_token'] not in downloaded_minutes and
            (download_type == 2 or minutes['object_type'] == download_type)
        ]
        print(f"需要下载 {len(need_download_minutes)} 个妙记")

        # 如果有需要下载的妙记则进行下载
        if need_download_minutes:
            self.download_minutes(need_download_minutes)
            # 将下载的妙记id写入记录
            with open('minutes.txt', 'a') as f:
                for minutes in need_download_minutes:
                    f.write(minutes['object_token']+'\n')
            print(f"成功下载了 {len(need_download_minutes)} 个妙记，等待 {check_interval} 秒后再次检查...")

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
        old_all_minutes = self.all_minutes
        successed_num = 0
        unsuccessed_num = 0
        for index in tqdm(old_all_minutes[:num+unsuccessed_num], desc='删除妙记'):
            old_minutes_num = self.minutes_num
            # 将该妙记放入回收站
            delete_url = f'https://meetings.feishu.cn/minutes/api/space/delete'
            params = {'object_tokens': index['object_token'],
                    'is_destroyed': 'false',
                    'language': 'zh_cn'}
            requests.post(url=delete_url, params=params, headers=self.headers, proxies=proxies)
            # 将该妙记彻底删除
            requests.post(url=delete_url, params=params.update({'is_destroyed': 'true'}), headers=self.headers, proxies=proxies)
            time.sleep(3)
            self.get_minutes()
            if self.minutes_num == old_minutes_num:
                print(f"删除 http://meetings.feishu.cn/minutes/{index['object_token']} 失败，可能是没有该妙记的权限")
                unsuccessed_num += 1
            else:
                successed_num += 1
            if successed_num == num:
                break
        print(f"成功删除 {successed_num} 个妙记，跳过 {unsuccessed_num} 个妙记")

if __name__ == '__main__':

    if not minutes_cookie:
        raise Exception("cookie不能为空！")
    
    # 定时检查是否有要下载的妙记
    while True:
        print(time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()))
        downloader = FeishuDownloader(minutes_cookie)
        # 检查是否存在需要下载的妙记
        downloader.check_minutes()
        # 如果云端的妙记数量超过了最大限制，则删除最早的几个妙记
        if downloader.minutes_num > vc_max_num:
            print(f"删除最早的 {downloader.minutes_num - vc_max_num} 个妙记")
            downloader.delete_minutes(downloader.minutes_num - vc_max_num)
        time.sleep(check_interval)

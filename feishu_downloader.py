import os, re, subprocess, time
from concurrent.futures import as_completed, ThreadPoolExecutor

import requests
from tqdm import tqdm


# 在飞书妙记主页 https://meetings.feishu.cn/minutes/home 获取该cookie
minutes_cookie = ""

# （可选，需身份为企业创建人、超级管理员或普通管理员）在飞书管理后台获取该cookie
manager_cookie = ""

space_name = 2 # 1:主页（包含归属人为自己的妙记，和别人共享给自己的妙记）; 2:我的内容（只包含归属人为自己的妙记）
download_type = 2 # 0:只下载会议妙记; 1:只下载自己上传的妙记; 2:下载所有妙记.

proxies = {'http': None, 'https': None}
# proxies = {'http': '127.0.0.1:7890', 'https': '127.0.0.1:7890'} # Python3.6
# proxies = {'http': 'http://127.0.0.1:7890', 'https': 'https://127.0.0.1:7890'} # Python3.7及以上


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
            raise Exception("cookie中不包含bv_csrf_token，请确保从请求`list?size=20&`中获取！")

        self.meeting_time_dict = {} # 会议文件名称和会议时间的对应关系
        
    def get_minutes(self):
        """
        批量获取妙记信息
        """
        get_rec_url = f'https://meetings.feishu.cn/minutes/api/space/list?&size=1000&space_name={space_name}'
        resp = requests.get(url=get_rec_url, headers=self.headers, proxies=proxies)
        return list(reversed(resp.json()['data']['list'])) # 返回按时间正序排列的妙记信息（从旧到新）

    def check_minutes(self):
        """
        检查需要下载的妙记
        """
        all_minutes = self.get_minutes()
        need_download_minutes = all_minutes
        
        # 检查记录中不存在的妙记id进行下载
        if os.path.exists('minutes.txt'):
            with open('minutes.txt', 'r') as f:
                downloaded_minutes = f.readlines()
            need_download_minutes = [minutes for minutes in need_download_minutes if minutes['object_token']+'\n' not in downloaded_minutes]

        # 如果只下载会议妙记，则过滤掉自己上传的妙记
        if download_type == 0:
            need_download_minutes = [minutes for minutes in need_download_minutes if minutes['object_type']==0]
        # 如果只下载自己上传的妙记，则过滤掉会议妙记
        elif download_type == 1:
            need_download_minutes = [minutes for minutes in need_download_minutes if minutes['object_type']==1]
        
        # 如果有需要下载的妙记则进行下载
        if need_download_minutes:
            self.download_minutes(need_download_minutes)
            # 将下载的妙记id写入记录
            with open('minutes.txt', 'a') as f:
                for minutes in need_download_minutes:
                    f.write(minutes['object_token']+'\n')
            print(f"成功下载了{len(need_download_minutes)}个妙记，等待下次检查...")

    def download_minutes(self, minutes_list):
        """
        使用aria2批量下载妙记
        """
        # 下载妙记视频
        with ThreadPoolExecutor(max_workers=10) as executor:
            with open('links.temp', 'w', encoding='utf-8') as file:
                futures = [executor.submit(self.get_minutes_url, minutes) for minutes in minutes_list]
                for future in as_completed(futures):
                    video_url = future.result()[0]
                    file_name = future.result()[1]
                    video_name = file_name
                    time_stamp = future.result()[2]
                    if time_stamp[-6] == '_':
                        video_name = video_name + '_' + time_stamp[-5:]
                    file.write(f'{video_url}\n out=data/{file_name}/{video_name}.mp4\n')

        headers_option = ' '.join(f'--header="{k}: {v}"' for k, v in self.headers.items())
        cmd = f'aria2c -c --input-file=links.temp {headers_option} --continue=true --auto-file-renaming=true --console-log-level=warn'
        subprocess.run(cmd, shell=True)

        # 删除临时文件
        os.remove('links.temp')

        # 修改会议妙记的创建时间
        for file_name, start_time in self.meeting_time_dict.items():
            os.utime(f'data/{file_name}', (start_time, start_time))
            os.utime(f'data/{file_name}/{file_name}.mp4', (start_time, start_time))
            os.utime(f'data/{file_name}/{file_name}.srt', (start_time, start_time))
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
        srt_url = f'https://meetings.feishu.cn/minutes/api/export'
        params = {'add_speaker': 'true', # 包含说话人
                   'add_timestamp': 'true', # 包含时间戳
                   'format': '3', # SRT格式
                   'object_token': minutes['object_token'], # 妙记id
                   }
        resp = requests.post(url=srt_url, params=params, headers=self.headers, proxies=proxies)
        resp.encoding = 'utf-8'

        # 获取妙记标题
        file_name = re.findall(r'filename="(.+)"', resp.headers['Content-Disposition'])[0][:-4]
        file_name = file_name.encode('iso-8859-1').decode('utf-8')
        rstr = r'[\/\\\:\*\?\"\<\>\|]'
        file_name = re.sub(rstr, '_', file_name)  # 将标题中的特殊字符替换为下划线
        
        # 如果妙记来自会议，则将会议起止时间作为文件名的一部分
        if minutes['object_type'] == 0:
            # 根据会议的起止时间和标题来设置文件名
            start_time = time.strftime("%Y年%m月%d日%H时%M分", time.localtime(minutes['start_time'] / 1000))
            stop_time = time.strftime("%Y年%m月%d日%H时%M分", time.localtime(minutes['stop_time'] / 1000))
            file_name = start_time+"至"+stop_time+file_name
            srt_name = file_name
        else:
            # 取当前时间戳后5位
            time_stamp = str(int(time.time() * 1000))[-5:]
            srt_name = file_name + '_' + time_stamp
            
        # 创建文件夹
        if not os.path.exists(f'data/{file_name}'):
            os.makedirs(f'data/{file_name}')

        # 写入字幕文件
        with open(f'data/{file_name}/{srt_name}.srt', 'w', encoding='utf-8') as f:
            f.write(resp.text)
        
        # 如果妙记来自会议，则记录会议起止时间
        if minutes['object_type'] == 1:
            self.meeting_time_dict[file_name] = minutes['start_time']/1000

        return video_url, file_name, srt_name

    def delete_minutes(self, num):
        """
        删除指定数量的最早几个妙记
        """
        all_minutes = self.get_minutes()
        num = num if num <= len(all_minutes) else 1
        need_delete_minutes = all_minutes[:num]

        for index in tqdm(need_delete_minutes, desc='删除妙记'):
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
            time.sleep(3600)

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
                if usage_bytes > 2 ** 30 * 9.65:
                    downloader.delete_minutes(2)
            usage_bytes_old = usage_bytes #　更新已用字节数
            time.sleep(3600)

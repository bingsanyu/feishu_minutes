import base64, configparser, time, uuid, zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import requests
from tqdm import tqdm


# 读取配置文件
config = configparser.ConfigParser(interpolation=None)
config.read('config.ini', encoding='utf-8')
# 获取cookie
minutes_cookie = config.get('Cookies', 'minutes_cookie')
# 获取文件路径
file_path = config.get('上传设置', '要上传的文件所在路径（目前仅支持单个文件）')
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

class FeishuUploader:
    def __init__(self, file_path, cookie):
        self.file_path = Path(file_path)
        if not self.file_path.is_file():
            raise ValueError(f"请选择有效的上传文件：{self.file_path}")
        self.block_size = 4 * 2**20
        self.headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
            'cookie': cookie,
            'bv-csrf-token': cookie[cookie.find('bv_csrf_token=') + len('bv_csrf_token='):cookie.find(';', cookie.find('bv_csrf_token='))],
            'referer': 'https://minutes.feishu.cn/minutes/home'
        }
        if len(self.headers.get('bv-csrf-token')) != 36:
            raise Exception("cookie中不包含bv_csrf_token，请确保从请求`list?size=20&`中获取！")

        self.upload_token = None
        self.vhid = None
        self.upload_id = None
        self.object_token = None

        self.file_size = self.file_path.stat().st_size
        if not self.file_size:
            raise ValueError("上传文件不能为空！")
        with self.file_path.open('rb') as file:
            self.file_header = base64.b64encode(file.read(512)).decode()

    @property
    def block_count(self):
        return (self.file_size + self.block_size - 1) // self.block_size

    def get_quota(self):
        file_info = f'{uuid.uuid1()}_{self.file_size}'
        quota_url = f'https://meetings.feishu.cn/minutes/api/quota?file_info[]={file_info}&language=zh_cn'
        quota_res = requests.get(quota_url, headers=self.headers, proxies=proxies).json()
        if quota_res['data']['has_quota'] == False:
            raise Exception("飞书妙记空间已满，请清理后重试！")
        self.upload_token = quota_res['data']['upload_token'][file_info]

    # 分片上传文件（预上传）
    # doc: https://open.feishu.cn/document/server-docs/docs/drive-v1/upload/multipart-upload-file-/upload_prepare
    def prepare_upload(self):
        prepare_url = f'https://meetings.feishu.cn/minutes/api/upload/prepare'
        json = {
            'name': self.file_path.stem,
            'file_size': self.file_size,
            'file_header': self.file_header,
            'drive_upload': True,
            'upload_token': self.upload_token,
        }
        response = requests.post(prepare_url, headers=self.headers, proxies=proxies, json=json)
        response.raise_for_status()
        payload = response.json()
        if payload.get('code') != 0:
            raise RuntimeError(f"飞书拒绝预上传（code={payload.get('code')}）")
        data = payload['data']
        self.block_size = int(data.get('block_size', self.block_size))
        if self.block_size <= 0 or int(data.get('num_blocks', self.block_count)) != self.block_count:
            raise ValueError("飞书预上传返回了无效的分片参数")
        self.vhid = data['vhid']
        self.upload_id = data['upload_id']
        self.object_token = data['object_token']

    # 分片上传文件（上传分片）
    # doc: https://open.feishu.cn/document/server-docs/docs/drive-v1/upload/multipart-upload-file-/upload_part
    def upload_blocks(self):
        if self.block_count == 1:
            with self.file_path.open('rb') as file, tqdm(total=1, unit='block') as progress:
                data = file.read(self.file_size + 1)
                if len(data) != self.file_size:
                    raise ValueError("上传过程中源文件大小发生变化")
                self.upload_block(0, data)
                progress.update(1)
            return

        workers = 6
        with self.file_path.open('rb') as file, ThreadPoolExecutor(max_workers=workers) as executor, \
                tqdm(total=self.block_count, unit='block') as progress:
            pending = set()
            try:
                for index in range(self.block_count):
                    expected = min(self.block_size, self.file_size - index * self.block_size)
                    data = file.read(expected)
                    if len(data) != expected:
                        raise ValueError("上传过程中源文件大小发生变化")
                    pending.add(executor.submit(self.upload_block, index, data))
                    if len(pending) == workers:
                        finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in finished:
                            future.result()
                            progress.update(1)
                if file.read(1):
                    raise ValueError("上传过程中源文件大小发生变化")
                for future in pending:
                    future.result()
                    progress.update(1)
            finally:
                for future in pending:
                    future.cancel()

    def upload_block(self, index, data):
        checksum = zlib.adler32(data) & 0xffffffff
        url = ('https://internal-api-space.feishu.cn/space/api/box/stream/upload/block'
               f'?upload_id={self.upload_id}&seq={index}&size={len(data)}&checksum={checksum}')
        response = requests.post(url, headers=self.headers, proxies=proxies, data=data)
        response.raise_for_status()
        code = response.json().get('code')
        if code != 0:
            raise RuntimeError(f"分片 {index} 上传失败（code={code}）")
    
    # 分片上传文件（完成上传）
    # doc: https://open.feishu.cn/document/server-docs/docs/drive-v1/upload/multipart-upload-file-/upload_finish
    def complete_upload(self):
        complete_url1 = f'https://internal-api-space.feishu.cn/space/api/box/upload/finish/'
        json = {
            'upload_id': self.upload_id,
            'num_blocks': self.block_count,
            'vhid': self.vhid,
            'risk_detection_extra' : '{\"source_terminal\":1,\"file_operate_usage\":3,\"locale\":\"zh_cn\"}'
        }
        resp = requests.post(complete_url1, headers=self.headers, proxies=proxies, json=json).json()
        print(resp)

        complete_url2 = f'https://meetings.feishu.cn/minutes/api/upload/finish'
        json = {
            'auto_transcribe': True,
            'language': 'mixed',
            'num_blocks': self.block_count,
            'upload_id': self.upload_id,
            'vhid': self.vhid,
            'upload_token': self.upload_token,
            'object_token': self.object_token,
        }
        resp = requests.post(complete_url2, headers=self.headers, proxies=proxies, json=json).json()
        print(resp)

        # 上传完成后检查是否转写完成
        start_time = time.time()
        while True:
            time.sleep(3)
            object_status_url = f'https://meetings.feishu.cn/minutes/api/batch-status?object_token[]={self.object_token}&language=zh_cn'
            object_status = requests.get(object_status_url, headers=self.headers, proxies=proxies).json()
            transcript_progress = object_status['data']['status'][0]['transcript_progress']
            spend_time = time.time() - start_time
            if object_status['data']['status'][0]['object_status'] == 2 or transcript_progress['current'] == '':
                print(f"\n转写完成！用时{spend_time}\nhttp://meetings.feishu.cn/minutes/{object_status['data']['status'][0]['object_token']}")
                break
            print(f"转写中...已用时{spend_time}\r", end='')

    def upload(self):
        self.get_quota()
        self.prepare_upload()
        self.upload_blocks()
        self.complete_upload()


if __name__ == '__main__':

    uploader = FeishuUploader(file_path, minutes_cookie)
    uploader.upload()

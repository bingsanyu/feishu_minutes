多线程上传/下载飞书妙记（SRT字幕）

## 使用场景

- 定期下载飞书会议视频与字幕，实现会议的自动备份。
- 定期检查妙记额度使用情况，快要超出则删除旧的妙记。
- 从本地上传视频后导出字幕，实现语音转文字。

## 使用步骤

1. 首先安装 request 库和 tqdm 库 `pip install requests tqdm`。
  
2. 打开[飞书妙记主页](https://meetings.feishu.cn/minutes/home)，按F12打开开发者工具，点击`网络`栏，刷新后复制网络请求 *list?size=20&space_name=* 中的`cookie`。

**下载妙记**

3. 将`步骤2`中的`cookie`粘贴至`feishu_downloader.py`的`minutes_cookie`变量处。
4. （可选）妙记余额不足才进行删除，以保证云端有尽量多的妙记：在完成`步骤3`的基础上，在[飞书管理后台](https://home.feishu.cn/admin/index)按F12，刷新后复制网络请求 *count?_t=* 中的`cookie`到代码文件中的变量`manager_cookie`处。
5. 执行 `python feishu_downloader.py`。

**上传妙记**

6. 将`步骤2`中的cookie粘贴至`feishu_uploader.py`的`cookie`变量处，并将所要上传的视频所在的路径填写至`file_path`变量处。
7. 执行 `python feishu_uploader.py`。注意：代码中仅为单个文件的上传。请勿滥用。

## 注意事项

- `feishu_downloader.py`中`space_name`的取值
  - `1`：主页（包含归属人为自己的妙记，和别人共享给自己的妙记）
  - `2`：我的内容（只包含归属人为自己的妙记）
- `feishu_downloader.py`中`is_meetings_only`的取值
  - `False`（默认）：会议生成的妙记和自己上传的妙记均下载
  - `True`：只下载会议生成的妙记
- `步骤2`中的`cookie`是以 *minutes_csrf_token=* 为开头的很长的一个字符串。
- `步骤4`中的`cookie`是以 *passport_web_did=* 为开头的很长的一个字符串。
- 是否使用代理可根据自己的情况自行设置。默认不使用。
- [飞书分片上传文件API](https://open.feishu.cn/document/server-docs/docs/drive-v1/upload/multipart-upload-file-/introduction) 中声明该接口不支持太高的并发且调用频率上限为5QPS
，且本人无批量转文字需求，故未对多个文件的同时转写进行尝试。本项目仅为实现上传与下载的自动化。

多线程上传/下载飞书妙记（SRT字幕）

## 使用场景

- 定期下载飞书会议视频与字幕，实现会议的自动备份。
- 定期检查妙记额度使用情况，快要超出则删除旧的妙记。
- 从本地上传视频后导出字幕，实现语音转文字。

## 使用步骤

1. 首先安装 requests 库和 tqdm 库 `pip install requests tqdm`。
  
2. 打开[飞书妙记主页](https://meetings.feishu.cn/minutes/home)，按F12打开开发者工具，点击`网络`栏，刷新后复制网络请求 *list?size=20&space_name=* 中的`cookie`，粘贴至`config.ini`的`minutes_cookie`。

**下载妙记**

3. （可选）妙记余额不足才进行删除，以保证云端有尽量多的妙记：在[飞书管理后台](https://home.feishu.cn/admin/index)按F12，刷新后复制网络请求 *count?_t=* 中的`cookie`，粘贴至`config.ini`的`manager_cookie`。
4. 根据自身需求修改`config.ini`中的参数。
5. 执行 `python feishu_downloader.py`。

**上传妙记**

3. 将要上传的视频路径填写到`config.ini`。
4. 执行 `python feishu_uploader.py`。注意：代码中仅为单个文件的上传。请勿滥用。

## 注意事项

- 下载需要用到aria2，本仓库中给出的是win64版本的，如果你是其他操作系统请在 https://github.com/aria2/aria2/releases 中下载相应版本并替换。
- `minutes_cookie`是以 *minutes_csrf_token=* 为开头的很长的一个字符串。
- `manager_cookie`是以 *passport_web_did=* 为开头的很长的一个字符串。
- [飞书分片上传文件API](https://open.feishu.cn/document/server-docs/docs/drive-v1/upload/multipart-upload-file-/introduction) 中声明该接口不支持太高的并发且调用频率上限为5QPS，且本人无批量转文字需求，故未对多个文件的同时转写进行尝试。本项目仅为实现上传与下载的自动化。

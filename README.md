多线程上传/下载飞书妙记（SRT字幕）

## 使用场景

- 定期下载飞书会议视频与字幕，实现会议的自动备份。
- 定期检查云端妙记数量，超出限制则删除旧的妙记。
- 从本地上传视频后导出字幕，实现语音转文字。

## 使用步骤（如果自己运行脚本）

1. 首先安装 requests 库和 tqdm 库 `pip install requests tqdm`。
  
2. 打开[飞书妙记主页](https://meetings.feishu.cn/minutes/home)，按F12打开开发者工具，点击`网络`栏，刷新后复制网络请求 *list?size=20&space_name=* 中的`cookie`，粘贴至`config.ini`的`cookie`。

**下载妙记**

3. 根据自身需求修改`config.ini`中的参数。
4. 执行 `python feishu_downloader.py`。

**上传妙记**

3. 将要上传的视频路径填写到`config.ini`。
4. 执行 `python feishu_uploader.py`。注意：代码中仅为单个文件的上传。请勿滥用。

## 注意事项

- 下载需要用到aria2，本仓库中给出的是win64版本的，如果你是其他操作系统请在 https://github.com/aria2/aria2/releases 中下载相应版本并替换。
- `cookie`是以 *minutes_csrf_token=* 为开头的很长的一个字符串。
- 因[飞书存储空间规则更新](https://www.feishu.cn/announcement/pricing-adjustment2024)后删除妙记不能及时更新额度，且妙记额度不再单独计算，故采用妙记数量（而不是按照已用额度）来判断是否要删除旧妙记。
- [飞书分片上传文件API](https://open.feishu.cn/document/server-docs/docs/drive-v1/upload/multipart-upload-file-/introduction) 中声明该接口不支持太高的并发且调用频率上限为5QPS，且本人无批量转文字需求，故未对多个文件的同时转写进行尝试。本项目仅为实现上传与下载的自动化。

# 运行声音参考

此目录保留准备好的 TTS 样本。当前 GPT-SoVITS 与 IndexTTS 都使用 C.wav（9 秒），由 config/voice-selection.json 选择，并由 references.json 提供文本及相对路径。不要删除当前运行样本。

A.wav 和 B.wav 保留作开发对照；qwen-ronova.wav 是已有云端音色的复刻样本，普通聊天不会重新上传它。原始录音和完整自动转写已迁出项目，不属于日常运行依赖。

更换本地样本或参考文本会影响声音；修改后需完整退出并重启。云端音色由 config/realtime-voice.json 单独控制，删除本地复刻记录不等于删除服务端资源。

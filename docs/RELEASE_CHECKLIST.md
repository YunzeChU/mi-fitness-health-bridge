# 公开发布检查表

发布前逐项确认：

- [ ] 工作树中没有真实导出、生成导入包、审计报告、IPA、截图或日志。
- [ ] `python -B -m unittest discover -s converter/tests -v` 通过。
- [ ] `python -B scripts/release_preflight.py` 通过。
- [ ] 审阅 `git status --short` 和待推送文件名。
- [ ] 审阅提交内容，确认没有本机绝对路径、账号/设备标识或真实测量值。
- [ ] CI 仅接收源码；不向 CI 提供任何个人导出。

发现疑似泄露时，应先停止提交和发布，确认受影响文件与历史，再决定是否撤回、重写历史或更换凭据。

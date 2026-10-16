"""Stage 4 console: async streaming text, preheated voice and incremental speech."""
import argparse
import json
from chat_client import ChatClient
from local_voice import LocalVoice
from reply_pipeline import ReplyPipeline
from voice_input import VoiceCall, input_devices
from voice_backends import BackendRouter, default_backend


def argument_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-voice", action="store_true", help="仅文字聊天")
    parser.add_argument("--call", choices=("headphones", "speakers", "speakers-aec"), help="启动持续中文语音模式")
    parser.add_argument("--mic", type=int, help="麦克风设备编号，见 /devices")
    parser.add_argument('--engine', choices=('gpt', 'qwen'), default=default_backend(), help='默认千问实时语音；gpt 为 GPT-SoVITS V2ProPlus')
    return parser


def main():
    parser = argument_parser()
    args = parser.parse_args()
    if args.no_voice and args.call:
        parser.error("--no-voice 与 --call 不能同时使用")
    def status(message):
        print(f"\n[状态] {message}", flush=True)
    router = BackendRouter(ChatClient(), LocalVoice(status=status), status=status)
    if args.engine != 'gpt':
        try: router.select(args.engine)
        except Exception as exc:
            router.chat.close(); router.voice.close()
            parser.error(str(exc))
    voice, chat = router.voice, router.chat
    pipeline = ReplyPipeline(chat, voice, output=lambda text: print(text, end="", flush=True),
                             status=status, voice_enabled=not args.no_voice)
    call = VoiceCall(pipeline, voice, output=lambda text: print(text, end="", flush=True), status=status)
    mic_device = args.mic
    print("若娜瓦聊天已启动。" + ("千问实时生成文字和语音，收到音频后立即播放。" if router.selected == 'qwen' else "文字逐步显示，句段到达就合成，生成后立即播放；语音会在后台提前准备。"))
    print("/voice on/off · /stop · /retry · /clear · /timings · /quit")
    print("/call on（扬声器） · /call aec（扬声器打断试验） · /call headphones（耳机） · /call off · /devices · /mic 编号")
    print('/engine gpt · /engine qwen · /engine（查看当前后端）；默认千问 Qwen-Audio-3.1-Realtime-Plus')
    status('当前语音：' + ('GPT-SoVITS V2ProPlus（CPU）' if router.selected == 'gpt' else 'Qwen-Audio-3.1-Realtime-Plus'))
    try:
        if not args.no_voice:
            try:
                voice.prepare()
            except Exception as exc:
                status(f"语音准备未启动（{type(exc).__name__}），文字聊天可继续")
        if args.call:
            call.start(args.call, mic_device)
        while True:
            try:
                user = input("\n你: ").strip()
            except KeyboardInterrupt:
                call.reset_input()
                pipeline.cancel()
                status("已停止本轮，输入 /quit 退出。")
                continue
            except EOFError:
                call.stop(cancel_reply=False)
                status("输入已结束，已停止收音；等待本轮语音播完，Ctrl+C 可停止退出。")
                try:
                    pipeline.wait_finished()
                    voice.wait_idle()
                except KeyboardInterrupt:
                    pipeline.cancel()
                break
            if not user:
                continue
            command = user.lower()
            if command in ("exit", "quit", "/quit"):
                break
            if command == "/stop":
                call.reset_input()
                pipeline.cancel(); status("已停止回应和朗读。")
            elif command == "/voice off":
                call.stop()
                pipeline.voice_enabled = False
                voice.stop(); status("语音已关闭。")
            elif command == "/voice on":
                pipeline.voice_enabled = True
                try:
                    voice.prepare(); status("语音已开启，准备将在后台完成。")
                except Exception as exc:
                    status(f"语音准备失败（{type(exc).__name__}）")
            elif command == "/clear":
                call.reset_input()
                pipeline.clear(); status("本次上下文已清除。")
            elif command == "/retry":
                call.reset_input()
                pipeline.cancel()
                if pipeline.last_reply:
                    pipeline.voice_enabled = True
                    try:
                        voice.speak(pipeline.last_reply)
                    except Exception as exc:
                        status(f"语音未启动（{type(exc).__name__}）")
                else:
                    status("还没有可重试的完整回复。")
            elif command == "/timings":
                print(json.dumps({**pipeline.timings(), "call": call.timings()}, ensure_ascii=False, indent=2), flush=True)
            elif command == '/engine':
                status('当前后端：' + router.selected)
            elif command.startswith('/engine '):
                target = command.split(maxsplit=1)[1]
                try:
                    router.validate(target)
                    if target != router.selected:
                        resume, mode = call.active, call.mode
                        call.stop()
                        if not voice.wait_idle(3):
                            raise RuntimeError('上一轮声音还未停止，请稍后重试')
                        router.select(target)
                        with pipeline._lock:
                            pipeline.last_reply = None
                            pipeline.last_turn = None
                        if pipeline.voice_enabled: voice.prepare()
                        if resume: call.start(mode, mic_device)
                    status('当前后端：' + router.selected + '；两套上下文分别保留在内存中。')
                except Exception as exc:
                    status(f'切换未完成：{exc}')
            elif command in ("/call on", "/call speakers", "/call headphones", "/call aec"):
                mode = 'speakers-aec' if command == '/call aec' else ('headphones' if command.endswith('headphones') else 'speakers')
                pipeline.voice_enabled = True
                try:
                    voice.prepare()
                    call.start(mode, mic_device)
                except Exception as exc:
                    status(f"通话未开启：{type(exc).__name__}: {exc}")
            elif command in ("/call off", "/mute"):
                call.stop(); status("已结束通话，停止收音和回应。")
            elif command == "/devices":
                try:
                    print(json.dumps(input_devices(), ensure_ascii=False, indent=2), flush=True)
                except Exception as exc:
                    status(f"读取麦克风设备失败：{type(exc).__name__}: {exc}")
            elif command.startswith("/mic "):
                try:
                    selection = command.split(maxsplit=1)[1]
                    device = None if selection == "default" else int(selection)
                    if device is not None and device not in {item["id"] for item in input_devices()}:
                        raise ValueError("该编号不是麦克风设备")
                    call.stop()
                    mic_device = device
                    status("麦克风已选择，输入 /call on 或 /call headphones 开始。")
                except (ValueError, OSError) as exc:
                    status(f"麦克风选择失败：{exc}")
            elif command.startswith("/"):
                status("未知命令。可用 /engine gpt/qwen、/call on/off、/call aec、/call headphones、/devices、/mic 编号、/voice on/off、/stop、/retry、/clear、/timings、/quit。")
            else:
                call.reset_input()
                pipeline.submit(user)
    finally:
        call.stop()
        pipeline.close()
        voice.close()
        print("已退出，临时对话音频已清理。")


if __name__ == "__main__":
    main()

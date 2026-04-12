from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult


class AdminCommands:
    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def op(self, event: AstrMessageEvent, admin_id: str = "") -> None:
        """授权管理员。op <admin_id>"""
        umo = event.unified_msg_origin
        if not admin_id:
            event.set_result(
                MessageEventResult().message(
                    "使用方法: /op <id> 授权管理员；/deop <id> 取消管理员。可通过 /sid 获取 ID。",
                ),
            )
            return
        cfg = self.context.get_config(umo=umo)
        cfg["admins_id"].append(str(admin_id))
        cfg.save_config()
        event.set_result(MessageEventResult().message("授权成功。"))

    async def deop(self, event: AstrMessageEvent, admin_id: str = "") -> None:
        """取消授权管理员。deop <admin_id>"""
        umo = event.unified_msg_origin
        cfg = self.context.get_config(umo=umo)
        if not admin_id:
            event.set_result(
                MessageEventResult().message(
                    "使用方法: /deop <id> 取消管理员。可通过 /sid 获取 ID。",
                ),
            )
            return
        try:
            cfg["admins_id"].remove(str(admin_id))
            cfg.save_config()
            event.set_result(MessageEventResult().message("取消授权成功。"))
        except ValueError:
            event.set_result(
                MessageEventResult().message("此用户 ID 不在管理员名单内。"),
            )

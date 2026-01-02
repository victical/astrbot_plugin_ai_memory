from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.event.filter import command, command_group
from astrbot.api import llm_tool
import os
import logging

from .memory_manager import MemoryManager
from .config_manager import ConfigManager

logger = logging.getLogger("astrbot")

@register("ai_memory", "kjqwdw、victical", "一个AI记忆管理插件", "1.0.5")
class Main(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.PLUGIN_NAME = "ai_memory"
        
        # 使用规范的插件数据目录
        plugin_data_dir = StarTools.get_data_dir()
        # 确保目录存在
        os.makedirs(plugin_data_dir, exist_ok=True)
        self.data_file = os.path.join(plugin_data_dir, "memory_data.json")
        
        # 初始化配置管理器
        default_config = {
            "max_memories": config.get("max_memories", 10),
            "auto_save_enabled": config.get("auto_save_enabled", True),
            "importance_threshold": config.get("importance_threshold", 3),
            "enable_memory_management": config.get("enable_memory_management", True),
            "enable_global_memory": config.get("enable_global_memory", False),
            "allowed_groups": config.get("allowed_groups", "")
        }
        self.config_manager = ConfigManager(default_config)
        
        # 初始化记忆管理器
        self.memory_manager = MemoryManager(self.data_file, self.config_manager.get_config())
        
        logger.info("AI记忆管理插件初始化完成")

    def _get_session_id(self, event: AstrMessageEvent) -> str:
        """获取统一的会话ID，全局模式下返回固定ID (仅限群聊)"""
        is_group = bool(event.get_group_id())
        if is_group and self.config_manager.get_config().get("enable_global_memory", False):
            return "global"
        if hasattr(event, 'unified_msg_origin'):
            return event.unified_msg_origin
        return str(event.session_id)

    @command_group("memory")
    def memory(self):
        """记忆管理指令组"""
        pass

    @memory.command("list")
    async def list_memories(self, event: AstrMessageEvent):
        """列出记忆。私聊下列出私聊记忆，群聊下根据全局开关列出群聊/全局记忆"""
        is_admin = event.role == "admin"
        group_id = event.get_group_id()
        is_private = not group_id
        is_global_mode = self.config_manager.get_config().get("enable_global_memory", False)

        # 管理员私聊模式：显示所有或全局
        if is_admin and is_private:
            all_memories = self.memory_manager.memories
            if not all_memories:
                return event.plain_result("📂 记忆数据库目前为空。")
            
            # 为了符合“私聊下使用memory list默认列出当前的私聊记忆”
            session_id = self._get_session_id(event)
            memories = self.memory_manager.get_memories_sorted(session_id)
            if memories:
                memory_text = "📝 当前私聊记忆:\n"
                for i, memory in enumerate(memories):
                    importance_stars = "⭐" * memory["importance"]
                    memory_text += f"{i+1}. {memory['content']}\n"
                    memory_text += f"   重要程度: {importance_stars} ({memory['importance']}/5)\n"
                    memory_text += f"   时间: {memory['timestamp']}\n\n"
                return event.plain_result(memory_text)
            else:
                return event.plain_result("当前私聊没有保存的记忆。可以使用 /memory list_all 查看所有记忆 (管理员)。")

        # 检查群组限制 (仅针对群聊)
        if group_id:
            allowed_groups_str = self.config_manager.get_config().get("allowed_groups", "")
            if allowed_groups_str.strip():
                allowed_groups = [g.strip() for g in allowed_groups_str.split(",") if g.strip()]
                if group_id not in allowed_groups:
                    return event.plain_result("🚫 该功能仅限在指定的群组中使用。")

        session_id = self._get_session_id(event)
        memories = self.memory_manager.get_memories_sorted(session_id)
        
        if not memories:
            return event.plain_result("当前会话没有保存的记忆。")
        
        prefix = "🌐 全局记忆" if (group_id and is_global_mode) else "📝 当前会话记忆"
        memory_text = f"{prefix}:\n"
        for i, memory in enumerate(memories):
            importance_stars = "⭐" * memory["importance"]
            memory_text += f"{i+1}. {memory['content']}\n"
            memory_text += f"   重要程度: {importance_stars} ({memory['importance']}/5)\n"
            memory_text += f"   时间: {memory['timestamp']}\n\n"
        
        return event.plain_result(memory_text)

    @memory.command("list_all")
    async def list_all_memories(self, event: AstrMessageEvent):
        """(管理员) 列出数据库中所有的记忆"""
        if event.role != "admin":
            return event.plain_result("🚫 仅管理员可使用此指令。")
        
        all_memories = self.memory_manager.memories
        if not all_memories:
            return event.plain_result("📂 记忆数据库目前为空。")

        memory_text = "📋 全部会话记忆详单 (管理员模式):\n\n"
        for session_id, memories in all_memories.items():
            memory_text += f"📍 会话: {session_id}\n"
            sorted_memories = sorted(memories, key=lambda x: x["importance"], reverse=True)
            for i, memory in enumerate(sorted_memories):
                importance_stars = "⭐" * memory["importance"]
                memory_text += f"  {i+1}. {memory['content']}\n"
                memory_text += f"     重要程度: {importance_stars}\n"
            memory_text += "\n"
        return event.plain_result(memory_text)

    @memory.command("list_group")
    async def list_group_memories(self, event: AstrMessageEvent, target_group_id: str = None):
        """查询群聊记忆"""
        is_global = self.config_manager.get_config().get("enable_global_memory", False)
        group_id = event.get_group_id()
        
        if target_group_id:
            target_id = target_group_id
            name = f"👥 群组 {target_group_id}"
        elif is_global:
            # 全局模式开启，群聊记忆即为 global 桶
            target_id = "global"
            name = "🌐 全局群聊"
        elif group_id:
            # 全局模式关闭，在群聊中则查看当前群
            target_id = group_id
            name = f"👥 群组 {group_id}"
        else:
            # 全局模式关闭，且在私聊中
            return event.plain_result("💡 全局记忆模式未开启。请指定群号或在群聊中使用。用法: /memory list_group [群号]")

        memories = self.memory_manager.get_memories_sorted(target_id)
        if not memories:
            return event.plain_result(f"📂 {name} 目前没有保存的记忆。")
        
        memory_text = f"📝 {name} 的记忆:\n"
        for i, memory in enumerate(memories):
            importance_stars = "⭐" * memory["importance"]
            memory_text += f"{i+1}. {memory['content']}\n"
            memory_text += f"   重要程度: {importance_stars} ({memory['importance']}/5)\n"
            memory_text += f"   时间: {memory['timestamp']}\n\n"
        
        return event.plain_result(memory_text)


    @memory.command("search")
    async def search_memories(self, event: AstrMessageEvent, keyword: str):
        """搜索记忆"""
        session_id = self._get_session_id(event)
        memories = self.memory_manager.search_memories(session_id, keyword)
        
        if not memories:
            return event.plain_result(f"没有找到包含 '{keyword}' 的记忆。")
        
        memory_text = f"🔍 搜索结果 (关键词: {keyword}):\n"
        for i, memory in enumerate(memories):
            importance_stars = "⭐" * memory["importance"]
            memory_text += f"{i+1}. {memory['content']}\n"
            memory_text += f"   重要程度: {importance_stars} ({memory['importance']}/5)\n"
            memory_text += f"   时间: {memory['timestamp']}\n\n"
        
        return event.plain_result(memory_text)

    @memory.command("stats")
    async def memory_stats(self, event: AstrMessageEvent):
        """显示记忆统计信息"""
        session_id = self._get_session_id(event)
        stats = self.memory_manager.get_memory_stats(session_id)
        
        if stats["total"] == 0:
            return event.plain_result("当前会话没有保存的记忆。")
        
        stats_text = "📊 记忆统计信息:\n"
        stats_text += f"总记忆数: {stats['total']}\n"
        stats_text += f"平均重要性: {stats['avg_importance']}/5\n"
        stats_text += "重要性分布:\n"
        
        for importance, count in stats["importance_distribution"].items():
            if count > 0:
                stars = "⭐" * importance
                stats_text += f"  {stars} ({importance}级): {count}条\n"
        
        return event.plain_result(stats_text)

    @memory.command("add")
    async def add_memory(self, event: AstrMessageEvent, content: str):
        """手动添加一条记忆。用法: /memory add <内容>"""
        session_id = self._get_session_id(event)
        
        content = str(content).strip()
        if not content:
            return event.plain_result("❌ 记忆内容不能为空。")
        
        importance = 3 # 手动添加默认设为3
        
        if self.memory_manager.add_memory(session_id, content, importance):
            await self.memory_manager.save_memories()
            importance_stars = "⭐" * importance
            return event.plain_result(f"✅ 已添加记忆: {content}\n重要程度: {importance_stars} ({importance}/5)\n💡 提示: 可使用 /memory update 指令修改重要性。")
        else:
            return event.plain_result("❌ 记忆管理功能已禁用，无法添加记忆。")

    @memory.command("edit")
    async def edit_memory(self, event: AstrMessageEvent, index: int, content: str):
        """编辑指定序号的记忆内容。用法: /memory edit <序号> <新内容>"""
        session_id = self._get_session_id(event)
        index = index - 1  # 用户输入1-based，转换为0-based
        
        memories = self.memory_manager.get_memories(session_id)
        if index < 0 or index >= len(memories):
            return event.plain_result("❌ 无效的记忆序号。")

        content = str(content).strip()
        if not content:
            return event.plain_result("❌ 记忆内容不能为空。")
        
        old_content = memories[index]["content"]
        memories[index]["content"] = content
            
        await self.memory_manager.save_memories()
        
        return event.plain_result(f"✅ 已编辑记忆 {index + 1}:\n原内容: {old_content}\n新内容: {content}\n💡 提示: 可使用 /memory update 指令修改重要性。")

    @memory.command("clear")
    async def clear_memories(self, event: AstrMessageEvent):
        """清空当前会话的所有记忆"""
        session_id = self._get_session_id(event)
        if self.memory_manager.clear_memories(session_id):
            await self.memory_manager.save_memories()
            return event.plain_result("✅ 已清空所有记忆。")
        return event.plain_result("当前会话没有保存的记忆。")

    @memory.command("remove")
    async def remove_memory(self, event: AstrMessageEvent, index: int):
        """删除指定序号的记忆"""
        session_id = self._get_session_id(event)
        index = index - 1  # 用户输入1-based，转换为0-based
        
        removed = self.memory_manager.remove_memory(session_id, index)
        if removed:
            await self.memory_manager.save_memories()
            return event.plain_result(f"✅ 已删除记忆: {removed['content']}")
        return event.plain_result("❌ 无效的记忆序号。")

    @memory.command("update")
    async def update_memory_importance(self, event: AstrMessageEvent, index: int, importance: int):
        """更新记忆的重要性"""
        session_id = self._get_session_id(event)
        index = index - 1  # 用户输入1-based，转换为0-based
        
        if importance < 1 or importance > 5:
            return event.plain_result("❌ 重要性必须在1-5之间。")
        
        if self.memory_manager.update_memory_importance(session_id, index, importance):
            await self.memory_manager.save_memories()
            return event.plain_result(f"✅ 已更新记忆重要性为 {importance}。")
        return event.plain_result("❌ 无效的记忆序号。")

    @command("memory_config")
    async def show_config(self, event: AstrMessageEvent):
        """显示当前配置"""
        summary = self.config_manager.get_config_summary()
        return event.plain_result(summary)

    @command("memory_reset_config")
    async def reset_config(self, event: AstrMessageEvent):
        """resets current config to default."""
        self.config_manager.reset_to_default()
        # 更新记忆管理器的配置
        self.memory_manager.config = self.config_manager.get_config()
        return event.plain_result("✅ 配置已重置为默认值")

    @command("mem_help")
    async def memory_help(self, event: AstrMessageEvent):
        """显示记忆插件帮助信息"""
        help_text = """🧠 记忆插件使用帮助：

📋 记忆管理指令：

🔍 查看记忆：
   /memory list - 列出当前会话的记忆(私聊独立，群聊受全局配置影响)
   /memory list_group - [群聊] 强制列出当前群聊的特定记忆
   /memory search <关键词> - 搜索包含关键词的记忆
   /memory stats - 显示记忆统计信息

✏️ 添加/编辑记忆：
   /memory add <内容> - 手动添加记忆(默认3级重要性)
   /memory edit <序号> <新内容> - 编辑记忆内容
   /memory update <序号> <重要性> - 修改记忆重要性(1-5)

🗑️ 删除记忆：
   /memory remove <序号> - 删除指定序号的记忆
   /memory clear - 清空当前会话的所有记忆

⚙️ 记忆特性：
   - 全局记忆开关仅对群聊生效，私聊始终是独立的。
   - 记忆按重要程度(1-5)排序，⭐表示重要性
   - AI会自动保存重要的信息并参考历史记忆

💡 使用建议：
   - 使用 /memory add 添加后，通过 /memory update 灵活调整权重。
        """
        
        return event.plain_result(help_text)

    @llm_tool(name="save_memory")
    async def save_memory(self, event: AstrMessageEvent, content: str, importance: int = 1):
        """保存一条记忆
        
        Args:
            content(string): 要保存的记忆内容
            importance(number): 记忆的重要程度，1-5之间
        """
        # 检查自动保存是否启用
        if not self.memory_manager.config.get("auto_save_enabled", True):
            return "自动保存记忆功能已禁用"
        
        # 检查重要性阈值
        threshold = self.memory_manager.config.get("importance_threshold", 3)
        if importance < threshold:
            return f"记忆重要性({importance})低于阈值({threshold})，未保存"
        
        session_id = self._get_session_id(event)
        
        if self.memory_manager.add_memory(session_id, content, importance):
            await self.memory_manager.save_memories()
            return f"✅ 我记住了: {content} (重要性: {importance}/5)"
        else:
            return "❌ 记忆管理功能已禁用，无法保存记忆"

    @llm_tool(name="get_memories")
    async def get_memories(self, event: AstrMessageEvent) -> str:
        """获取当前会话的所有记忆"""
        session_id = self._get_session_id(event)
        memories = self.memory_manager.get_memories_sorted(session_id)
        
        if not memories:
            return "我没有任何相关记忆。"
        
        memory_text = "💭 相关记忆：\n"
        for i, memory in enumerate(memories[:5]):  # 只显示前5条最重要的记忆
            importance_stars = "⭐" * memory["importance"]
            memory_text += f"{i+1}. {memory['content']} ({importance_stars})\n"
        
        if len(memories) > 5:
            memory_text += f"\n... 还有 {len(memories) - 5} 条记忆"
        
        return memory_text

    @llm_tool(name="search_memories")
    async def search_memories_tool(self, event: AstrMessageEvent, keyword: str) -> str:
        """搜索记忆
        
        Args:
            keyword(string): 搜索关键词
        """
        session_id = self._get_session_id(event)
        memories = self.memory_manager.search_memories(session_id, keyword)
        
        if not memories:
            return f"没有找到包含 '{keyword}' 的记忆。"
        
        memory_text = f"🔍 搜索 '{keyword}' 的结果：\n"
        for i, memory in enumerate(memories[:3]):  # 只显示前3条结果
            importance_stars = "⭐" * memory["importance"]
            memory_text += f"{i+1}. {memory['content']} ({importance_stars})\n"
        
        if len(memories) > 3:
            memory_text += f"\n... 还有 {len(memories) - 3} 条相关记忆"
        
        return memory_text

    @llm_tool(name="get_memory_stats")
    async def get_memory_stats_tool(self, event: AstrMessageEvent) -> str:
        """获取记忆统计信息"""
        session_id = self._get_session_id(event)
        stats = self.memory_manager.get_memory_stats(session_id)
        
        if stats["total"] == 0:
            return "当前会话没有任何记忆。"
        
        stats_text = f"📊 记忆统计：共 {stats['total']} 条记忆，平均重要性 {stats['avg_importance']}/5"
        
        # 添加重要性分布
        importance_text = []
        for importance, count in stats["importance_distribution"].items():
            if count > 0:
                stars = "⭐" * importance
                importance_text.append(f"{stars}: {count}条")
        
        if importance_text:
            stats_text += f"\n重要性分布: {', '.join(importance_text)}"
        
        return stats_text

    async def on_config_update(self, new_config: dict):
        """配置更新时的回调"""
        # 更新配置管理器
        updated_config = self.config_manager.update_config(new_config)
        
        # 更新记忆管理器的配置
        self.memory_manager.config = updated_config
        
        logger.info(f"记忆插件配置已更新: {updated_config}")

    async def terminate(self):
        """插件卸载时的清理工作"""
        await self.memory_manager.save_memories()
        logger.info("AI记忆管理插件已卸载")

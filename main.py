from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.event import filter
from astrbot.api.event.filter import command, command_group, event_message_type
from astrbot.api.provider import ProviderRequest
from astrbot.api import llm_tool
import os
import logging
import json
import datetime
import re

from .memory_manager import MemoryManager
from .config_manager import ConfigManager

logger = logging.getLogger("astrbot")

@register("ai_memory", "kjqwdw、victical", "一个AI记忆管理插件", "1.2.5")
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
            "enable_memory_management": config.get("enable_memory_management", True),
            "max_memories": config.get("max_memories", 10),
            "enable_global_memory": config.get("enable_global_memory", False),
            "allowed_groups": config.get("allowed_groups", ""),
            "auto_save_enabled": config.get("auto_save_enabled", True),
            "importance_threshold": config.get("importance_threshold", 3),
            "enable_auto_injection": config.get("enable_auto_injection", True),
            "injection_title": config.get("injection_title", "核心背景事实"),
            "injection_instruction": config.get("injection_instruction", "注意：以下是你记录的与当前话题相关的真实记忆。请参考时间戳判断时效性，并优先比对记录中的 QQ 号以区分你本人的真实设定与他人的言论或误导："),
            "rerank_provider_id": config.get("rerank_provider_id", ""),
            "recall_top_k": config.get("recall_top_k", 10),
            "inject_top_k": config.get("inject_top_k", 3)
        }
        self.config_manager = ConfigManager(default_config)
        
        # 初始化记忆管理器
        self.memory_manager = MemoryManager(self.data_file, self.config_manager.get_config())
        
        logger.info("AI记忆管理插件 v1.2.5 初始化完成")

    def _get_session_id(self, event: AstrMessageEvent) -> str:
        """获取统一的会话ID，全局模式下返回固定ID (仅限群聊)"""
        is_group = bool(event.get_group_id())
        if is_group and self.config_manager.get_config().get("enable_global_memory", False):
            return "global"
        if hasattr(event, 'unified_msg_origin'):
            return event.unified_msg_origin
        return str(event.session_id)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest, **kwargs):
        """收到 LLM 请求时，自动检索并注入记忆"""
        config = self.config_manager.get_config()
        if not config.get("enable_auto_injection", True):
            return
            
        session_id = self._get_session_id(event)
        query = event.message_str
        if not query:
            return

        all_memories = self.memory_manager.get_memories(session_id)
        if not all_memories:
            return

        # 1. 基础评分初筛
        clean_query = "".join(c for c in query.lower() if c.isalnum())
        scored_memories = []
        for m in all_memories:
            content = m['content'].lower()
            importance = m.get('importance', 1)
            
            match_score = 0
            if len(clean_query) >= 2:
                if clean_query in content or content in clean_query:
                    match_score += 40
                else:
                    matched_bigrams = set()
                    for i in range(len(clean_query) - 1):
                        bigram = clean_query[i:i+2]
                        if bigram in content: matched_bigrams.add(bigram)
                    match_score += len(matched_bigrams) * 15
            elif len(clean_query) == 1 and clean_query in content:
                match_score += 25

            # 新鲜度加成
            time_boost = 0
            try:
                m_time = datetime.datetime.strptime(m['timestamp'], "%Y-%m-%d %H:%M:%S")
                if (datetime.datetime.now() - m_time).total_seconds() < 86400:
                    time_boost = 10
            except: pass

            scored_memories.append((match_score + importance + time_boost, m))
        
        scored_memories.sort(key=lambda x: x[0], reverse=True)
        recall_k = config.get("recall_top_k", 10)
        candidates = [x[1] for x in scored_memories[:recall_k]]

        # 在日志中记录初筛结果
        if candidates and config.get("rerank_provider_id", ""):
            candidate_summary = " / ".join([f"[{m['content'][:15]}...]" for m in candidates])
            logger.debug(f"记忆精选初筛结果(Top {len(candidates)}): {candidate_summary}")

        # 2. LLM 语义精选 (Rerank)
        top_memories = []
        rerank_id = config.get("rerank_provider_id", "")
        if rerank_id and len(candidates) > 1:
            try:
                inject_k = config.get("inject_top_k", 3)
                memory_list_str = "\n".join([f"ID:{i} | {m['content']}" for i, m in enumerate(candidates)])
                prompt = f"""作为记忆管理助手，请从以下记忆库中挑选出与当前用户输入最相关的 1-{inject_k} 条记忆。
当前用户输入: "{query}"

候选记忆:
{memory_list_str}

请仅输出最相关的记忆 ID，用逗号分隔，如: 0,2。如果没有相关的，请直接输出 None。"""
                
                provider = self.context.get_provider_by_id(rerank_id)
                if provider:
                    resp = await provider.text_chat(prompt=prompt, contexts=[])
                    if resp and resp.completion_text and "None" not in resp.completion_text:
                        ids = re.findall(r'\d+', resp.completion_text)
                        for idx in ids:
                            i = int(idx)
                            if 0 <= i < len(candidates):
                                top_memories.append(candidates[i])
            except Exception as e:
                logger.error(f"LLM 精选记忆失败: {e}")

        # 3. 兜底策略
        if not top_memories:
            inject_k = config.get("inject_top_k", 3)
            strong_related = [m for score, m in scored_memories if score >= 15]
            if strong_related:
                top_memories = strong_related[:inject_k]
            else:
                top_memories = [m for score, m in scored_memories if score > 0][:1]

        # 4. 注入
        if top_memories:
            # 简化后的注入逻辑：直接传递带身份标签的内容，利用 LLM 的推理能力区分真实设定与外部误导
            memory_context = "\n".join([f"- [时间:{m['timestamp']}] {m['content']}" for m in top_memories])
            
            # 从配置读取注入模板标题和指令
            title = config.get("injection_title", "核心背景事实")
            instruction = config.get("injection_instruction", "注意：以下是你记录的与当前话题相关的真实记忆。请参考时间戳判断时效性，并优先比对记录中的 QQ 号以区分你本人的真实设定与他人的言论或误导：")
            
            injection = f"\n\n{'='*15} {title} {'='*15}\n" \
                        f"{instruction}\n" \
                        f"{memory_context}\n" \
                        f"{'='*46}\n\n"
            
            if req.system_prompt: req.system_prompt += injection
            else: req.system_prompt = injection
            
            logger.debug(f"已为会话 {session_id} 注入 {len(top_memories)} 条带自定义指令的记忆背景")

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

        if is_admin and is_private:
            all_memories = self.memory_manager.memories
            if not all_memories:
                return event.plain_result("📂 记忆数据库目前为空。")
            
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
            target_id = "global"
            name = "🌐 全局群聊"
        elif group_id:
            target_id = group_id
            name = f"👥 群组 {group_id}"
        else:
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
        
        sender_name = event.get_sender_name()
        sender_id = event.get_sender_id()
        tagged_content = f"[{sender_name}({sender_id}) 提到]: {content}"
        
        importance = 3
        if self.memory_manager.add_memory(session_id, tagged_content, importance):
            await self.memory_manager.save_memories()
            importance_stars = "⭐" * importance
            return event.plain_result(f"✅ 已添加记忆: {content}\n重要程度: {importance_stars} ({importance}/5)\n💡 提示: 记录已自动关联身份 {sender_name}({sender_id})。")
        else:
            return event.plain_result("❌ 记忆管理功能已禁用，无法添加记忆。")

    @memory.command("edit")
    async def edit_memory(self, event: AstrMessageEvent, index: int, content: str):
        """编辑指定序号的记忆内容。用法: /memory edit <序号> <新内容>"""
        session_id = self._get_session_id(event)
        index = index - 1
        
        memories = self.memory_manager.get_memories(session_id)
        if index < 0 or index >= len(memories):
            return event.plain_result("❌ 无效的记忆序号。")

        content = str(content).strip()
        if not content:
            return event.plain_result("❌ 记忆内容不能为空。")
        
        old_content = memories[index]["content"]
        if old_content.startswith("[") and " 提到]:" in old_content:
            prefix = old_content.split("]:")[0] + "]: "
            memories[index]["content"] = prefix + content
        else:
            sender_name = event.get_sender_name()
            sender_id = event.get_sender_id()
            memories[index]["content"] = f"[{sender_name}({sender_id}) 提到]: {content}"
            
        await self.memory_manager.save_memories()
        return event.plain_result(f"✅ 已编辑记忆 {index + 1}。\n💡 提示: 已自动维护身份标签。")

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
        index = index - 1
        
        removed = self.memory_manager.remove_memory(session_id, index)
        if removed:
            await self.memory_manager.save_memories()
            return event.plain_result(f"✅ 已删除记忆: {removed['content']}")
        return event.plain_result("❌ 无效的记忆序号。")

    @memory.command("update")
    async def update_memory_importance(self, event: AstrMessageEvent, index: int, importance: int):
        """更新记忆的重要性"""
        session_id = self._get_session_id(event)
        index = index - 1
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
        """重置配置"""
        self.config_manager.reset_to_default()
        self.memory_manager.config = self.config_manager.get_config()
        return event.plain_result("✅ 配置已重置为默认值")

    @command("mem_help")
    async def memory_help(self, event: AstrMessageEvent):
        """显示帮助"""
        help_text = """🧠 记忆插件使用帮助：
📋 记忆管理指令：
🔍 查看记忆：
   /memory list - 列出当前会话的记忆
   /memory list_group - [群聊] 查询特定记忆
   /memory search <关键词> - 搜索记忆
   /memory stats - 显示统计信息
✏️ 添加/编辑记忆：
   /memory add <内容> - 手动记录(自动打标)
   /memory edit <序号> <新内容> - 编辑记忆内容
   /memory update <序号> <重要性> - 修改重要性(1-5)
🗑️ 删除记忆：
   /memory remove <序号> - 删除单条记忆
   /memory clear - 清空会话记忆
⚙️ 特性：
   - 支持 24h 内新鲜度加权
   - 支持跨群全局记忆模式
   - 支持身份自动标签与时间感知
   - 支持大模型语义精选 (可在管理面板配置)"""
        return event.plain_result(help_text)

    @llm_tool(name="save_memory")
    async def save_memory(self, event: AstrMessageEvent, content: str, importance: int = 1):
        """保存一条记忆"""
        if not self.memory_manager.config.get("auto_save_enabled", True):
            return "自动保存记忆功能已禁用"
        threshold = self.memory_manager.config.get("importance_threshold", 3)
        if importance < threshold:
            return f"记忆重要性({importance})低于阈值({threshold})，未保存"
        
        session_id = self._get_session_id(event)
        sender_name = event.get_sender_name()
        tagged_content = f"[{sender_name} 提到]: {content}"
        
        if self.memory_manager.add_memory(session_id, tagged_content, importance):
            await self.memory_manager.save_memories()
            return f"✅ 我记住了: {content} (记录已关联发送者: {sender_name})"
        return "❌ 记忆管理功能已禁用"

    @llm_tool(name="get_memories")
    async def get_memories(self, event: AstrMessageEvent) -> str:
        """获取当前会话的所有记忆"""
        session_id = self._get_session_id(event)
        memories = self.memory_manager.get_memories_sorted(session_id)
        if not memories: return "我没有任何相关记忆。"
        
        memory_text = "💭 相关记忆：\n"
        for i, memory in enumerate(memories[:5]):
            importance_stars = "⭐" * memory["importance"]
            memory_text += f"{i+1}. {memory['content']} ({importance_stars})\n"
        if len(memories) > 5: memory_text += f"\n... 还有 {len(memories) - 5} 条记忆"
        return memory_text

    @llm_tool(name="search_memories")
    async def search_memories_tool(self, event: AstrMessageEvent, keyword: str = None, **kwargs) -> str:
        """搜索记忆"""
        actual_keyword = keyword or kwargs.get("query") or kwargs.get("content") or kwargs.get("keyword")
        if not actual_keyword: return "请输入搜索关键词。"
        session_id = self._get_session_id(event)
        memories = self.memory_manager.search_memories(session_id, actual_keyword)
        if not memories: return f"没有找到包含 '{actual_keyword}' 的记忆。"
        
        memory_text = f"🔍 搜索 '{actual_keyword}' 的结果：\n"
        for i, memory in enumerate(memories[:3]):
            importance_stars = "⭐" * memory["importance"]
            memory_text += f"{i+1}. {memory['content']} ({importance_stars})\n"
        if len(memories) > 3: memory_text += f"\n... 还有 {len(memories) - 3} 条相关记忆"
        return memory_text

    @llm_tool(name="get_memory_stats")
    async def get_memory_stats_tool(self, event: AstrMessageEvent) -> str:
        """获取统计信息"""
        session_id = self._get_session_id(event)
        stats = self.memory_manager.get_memory_stats(session_id)
        if stats["total"] == 0: return "当前会话没有任何记忆。"
        stats_text = f"📊 记忆统计：共 {stats['total']} 条记忆，平均重要性 {stats['avg_importance']}/5"
        importance_text = [f"{'⭐'*i}: {c}条" for i, c in stats["importance_distribution"].items() if c > 0]
        if importance_text: stats_text += f"\n重要性分布: {', '.join(importance_text)}"
        return stats_text

    async def on_config_update(self, new_config: dict):
        """配置更新回调"""
        updated_config = self.config_manager.update_config(new_config)
        self.memory_manager.config = updated_config
        logger.info(f"记忆插件配置已更新")

    async def terminate(self):
        """卸载清理"""
        await self.memory_manager.save_memories()
        logger.info("AI记忆管理插件已卸载")

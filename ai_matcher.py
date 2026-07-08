# -*- coding: utf-8 -*-
"""微信抢单系统 - AI 智能匹配引擎
负责消息分析和抢单决策:
  第一层: 快速关键词匹配 (毫秒级)
  第二层: AI 智能识别 (需启用)
  第三层: 模式过滤 (单项/多项)
"""
import json, re, os, sys, time
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 科目类型关键词映射
TYPE_KEYWORDS = {
    "词汇": ["词汇", "单词", "四级", "六级", "4级", "6级", "4.6级", "4.6"],
    "阅读": ["阅读"],
    "语法": ["语法"],
    "完型": ["完型", "完形"],
    "听口": ["听口", "听力", "口语"],
    "写作": ["写作", "作文"],
    "抗遗忘": ["抗遗忘"],
}

# 否定词（如果有，跳过关键词层直接走AI判断）
NEGATION_WORDS = ["取消", "暂停", "停止", "不需要", "不要", "没有", "不抢", "已接", "已出"]

# 接单意图词（帮助关键词层更准确地判断）
ORDER_INTENT_WORDS = [
    "寻", "教练", "老师", "接单", "代课", "正课", "上课", "学生",
    "每周", "每次", "现结", "有耐心", "亲和力", "幽默", "基础",
    "初一", "初二", "初三", "高一", "高二", "高三", "准高一", "新初一", "小学",
    "年级", "男生", "女生", "晚上", "下午", "周末", "周日", "周六", "上课时间",
]

STRONG_ORDER_PATTERNS = [
    "寻代课教练", "寻正课教练", "寻正课老师", "正课老师", "正课教练",
    "代课教练", "需要带过", "上课时间", "每周", "现结", "每次一小时",
]

# AI 系统提示词
AI_SYSTEM_PROMPT = """你是微信抢单系统的智能识别引擎。

核心职责: 判断微信群消息是否为"家教/辅导/培训类接单信息"。

输出JSON格式:
{
  "is_order": true/false,
  "confidence": 0.95,
  "types": ["词汇", "阅读"],
  "student_info": "高中男生 基础中等",
  "time_info": "周三晚上8点 每次1小时",
  "extracted_day": 3,
  "extracted_start": "20:00",
  "extracted_duration": 60,
  "is_recurring": true,
  "default_type": false
}

规则:
1. is_order: 是否属于接单信息
2. confidence: 0-1 置信度
3. types: 科目类型数组，仅限: 词汇/阅读/语法/完型/听口/写作/抗遗忘
4. student_info: 从消息中提取的学生描述
5. time_info: 时间信息原文
6. extracted_day: 1=周一...7=周日，无法确定则为null
7. extracted_start: HH:mm格式开始时间
8. extracted_duration: 分钟数
9. is_recurring: 是否每周重复 (含"每周""每""平时"等)
10. default_type: 判定为接单但无法提取类型时为true

宁抢勿漏: 有疑虑时倾向于判断为接单。"""


class OrderMatcher:
    """订单匹配器 - 封装三层匹配逻辑"""
    
    def __init__(self, config_getter):
        """
        config_getter: 函数，接受 key 返回 value，用于实时读取配置
        """
        self.get_cfg = config_getter
    
    def match(self, message_text: str) -> dict:
        """
        对消息执行完整匹配流程
        返回: {matched: bool, type: str, method: str, extracted: dict}
        """
        msg = message_text.strip()
        if not msg:
            return self._no_match()

        # Fallback for newer WeChat versions: UI changed but text cannot be read.
        # Only enabled when config auto_reply_on_screen_change=true.
        if msg.startswith('[SCREEN_CHANGE]'):
            if self.get_cfg('auto_reply_on_screen_change') == 'true':
                default_type = self.get_cfg('ai_default_type') or '\u8bcd\u6c47'
                return {
                    'matched': True,
                    'type': default_type,
                    'types': [default_type],
                    'method': 'screen_change',
                    'confidence': 0.55,
                    'extracted': {'types': [default_type], 'default_type': True}
                }
            return self._no_match()
        
        # 检查否定词
        has_negation = any(w in msg for w in NEGATION_WORDS)
        
        # ===== 第一层: 快速关键词 =====
        if not has_negation:
            kw_result = self._keyword_match(msg)
            if kw_result["matched"]:
                # 再检查接单意图（避免误匹配闲聊中的关键词）
                if self._has_order_intent(msg):
                    return kw_result
        
        # ===== 第二层: 强接单意图兜底 (无类型但明显是接单) =====
        if not has_negation and self._strong_order_intent(msg):
            default_type = self.get_cfg("ai_default_type") or "词汇"
            return {
                "matched": True,
                "type": default_type,
                "types": [default_type],
                "method": "rule_intent",
                "confidence": 0.82,
                "extracted": {"types": [default_type], "default_type": True}
            }
        
        # ===== 第三层: AI 智能识别 =====
        if self.get_cfg("ai_enabled") == "true":
            ai_result = self._ai_match(msg)
            if ai_result["matched"]:
                return ai_result
        
        return self._no_match()
    
    def _keyword_match(self, msg: str) -> dict:
        """关键词快速匹配"""
        matched_types = []
        for type_name, keywords in TYPE_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in msg.lower():
                    if type_name not in matched_types:
                        matched_types.append(type_name)
                    break
        
        if matched_types:
            return {
                "matched": True,
                "type": matched_types[0],  # 主要类型
                "types": matched_types,
                "method": "keyword",
                "confidence": 0.95,
                "extracted": {"types": matched_types}
            }
        
        return self._no_match()
    
    def _ai_match(self, msg: str) -> dict:
        """AI 智能识别"""
        try:
            api_url = self.get_cfg("ai_api_url") or "https://api.openai.com/v1/chat/completions"
            api_key = self.get_cfg("ai_api_key") or ""
            model = self.get_cfg("ai_model") or "gpt-4o-mini"
            
            if not api_key:
                print("[AI] API Key 未配置，跳过 AI 匹配")
                return self._no_match()
            
            import urllib.request
            
            payload = json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": AI_SYSTEM_PROMPT},
                    {"role": "user", "content": f"判断以下微信群消息: \n{msg}"}
                ],
                "temperature": 0.1,
                "max_tokens": 300
            }).encode("utf-8")
            
            req = urllib.request.Request(api_url, data=payload, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            })
            
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                content = result["choices"][0]["message"]["content"]
            
            # 提取 JSON
            json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
            if not json_match:
                return self._no_match()
            
            ai_data = json.loads(json_match.group())
            
            if not ai_data.get("is_order", False):
                return self._no_match()
            
            types = ai_data.get("types", [])
            is_default = ai_data.get("default_type", False)
            
            if not types and is_default:
                default_type = self.get_cfg("ai_default_type") or "词汇"
                types = [default_type]
            
            if not types:
                types = ["词汇"]
            
            return {
                "matched": True,
                "type": types[0],
                "types": types,
                "method": "ai",
                "confidence": ai_data.get("confidence", 0.8),
                "extracted": {
                    "types": types,
                    "student_info": ai_data.get("student_info", ""),
                    "time_info": ai_data.get("time_info", ""),
                    "extracted_day": ai_data.get("extracted_day"),
                    "extracted_start": ai_data.get("extracted_start"),
                    "extracted_duration": ai_data.get("extracted_duration"),
                    "is_recurring": ai_data.get("is_recurring", False),
                }
            }
        except Exception as e:
            print(f"[AI] 匹配失败: {e}")
            return self._no_match()
    
    def _has_order_intent(self, msg: str) -> bool:
        """检查消息是否包含接单意图词"""
        return any(w in msg for w in ORDER_INTENT_WORDS)
    
    def _strong_order_intent(self, msg: str) -> bool:
        """无类型但明显是接单消息时兜底：默认视为词汇抢单"""
        if any(p in msg for p in STRONG_ORDER_PATTERNS):
            return True
        student_words = ["初一", "初二", "初三", "高一", "高二", "高三", "准高一", "新初一", "小学", "男生", "女生"]
        time_words = ["周一", "周二", "周三", "周四", "周五", "周六", "周日", "晚上", "下午", "点", "上课"]
        return any(w in msg for w in student_words) and any(w in msg for w in time_words)
    
    def _no_match(self) -> dict:
        return {"matched": False, "type": "", "types": [], "method": "", "confidence": 0, "extracted": {}}
    
    def apply_mode_filter(self, match_result: dict) -> bool:
        """
        应用模式过滤
        返回: True=应该抢单, False=不抢
        """
        if not match_result["matched"]:
            return False
        
        grab_mode = self.get_cfg("grab_mode") or "multi"
        
        if grab_mode == "multi":
            return True  # 多项模式，见单就抢
        
        # 单项模式：检查类型是否匹配
        selected_str = self.get_cfg("selected_types") or "[]"
        try:
            selected = json.loads(selected_str)
        except json.JSONDecodeError:
            selected = []
        
        if not selected:
            return True  # 没选类型默认全抢
        
        match_types = match_result.get("types", [])
        return any(t in selected for t in match_types)


# ==================== 便捷函数 ====================
def create_matcher():
    """创建匹配器实例"""
    import database as db
    return OrderMatcher(db.get_config)


# ==================== 测试 ====================
if __name__ == "__main__":
    import database as db
    db.init_db()
    matcher = OrderMatcher(db.get_config)
    
    test_messages = [
        "正课老师 高中，男生 4.6级词汇，阅读，语法 周日，周三晚上8点，每次1个小时",
        "寻代课教练，七组半小时的抗遗忘，现结。学生会自己读，很好带。",
        "今天20:30到21:30 词汇课，现结",
        "大家好今天天气不错",  # 不是接单
        "寻正课教练 小学三年级，每周五晚上7点半和周日晚上8点上课",
        "新初一，男生 每周二四晚8点 寻幽默风趣，善于鼓励人有亲和力的教练",
    ]
    
    for msg in test_messages:
        result = matcher.match(msg)
        status = "抢!" if result["matched"] else "略过"
        print(f"[{status}] {msg[:50]}... -> type={result.get('type','')} method={result.get('method','')}")


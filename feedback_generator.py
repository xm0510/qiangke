# -*- coding: utf-8 -*-
"""Course feedback generation with a free template fallback and optional AI polish."""
import json
import urllib.request

RATING_TEXT = {
    1: "\u9700\u8981\u660e\u663e\u6539\u8fdb",
    2: "\u5076\u6709\u5206\u5fc3",
    3: "\u6574\u4f53\u7a33\u5b9a",
    4: "\u8868\u73b0\u8f83\u597d",
    5: "\u8868\u73b0\u975e\u5e38\u597d",
}
MASTERY_TEXT = {
    1: "\u5c1a\u672a\u638c\u63e1",
    2: "\u90e8\u5206\u7406\u89e3",
    3: "\u57fa\u672c\u638c\u63e1",
    4: "\u638c\u63e1\u8f83\u597d",
    5: "\u638c\u63e1\u624e\u5b9e",
}

def _clean(value):
    return str(value or "").strip()

def build_template_feedback(data):
    student = _clean(data.get("student_name")) or "\u5b66\u751f"
    feedback_date = _clean(data.get("feedback_date"))
    kind = data.get("feedback_kind") or "main"
    content = _clean(data.get("class_content"))
    problems = _clean(data.get("problems"))
    homework = _clean(data.get("homework"))
    next_plan = _clean(data.get("next_plan"))
    notes = _clean(data.get("teacher_notes"))
    focus = RATING_TEXT.get(int(data.get("focus_rating") or 3), RATING_TEXT[3])
    mastery = MASTERY_TEXT.get(int(data.get("mastery_rating") or 3), MASTERY_TEXT[3])
    if kind == "review":
        count = max(1, int(data.get("review_count") or len(data.get("schedule_entry_ids") or []) or 1))
        opening = f"{student}\u5728{feedback_date}\u5b8c\u6210\u4e86{count}\u6b21\u6297\u9057\u5fd8\u590d\u4e60\u3002"
        if content:
            opening += f"\u672c\u6b21\u4e3b\u8981\u590d\u4e60\uff1a{content}\u3002"
        state = f"\u590d\u4e60\u8fc7\u7a0b\u4e2d\u4e13\u6ce8\u8868\u73b0{focus}\uff0c\u77e5\u8bc6\u4fdd\u6301\u60c5\u51b5\u4e3a{mastery}\u3002"
    else:
        opening = f"{student}\u5728{feedback_date}\u5b8c\u6210\u4e86\u672c\u6b21\u8bfe\u7a0b\u3002"
        if content:
            opening += f"\u672c\u8282\u8bfe\u4e3b\u8981\u5b66\u4e60\uff1a{content}\u3002"
        state = f"\u8bfe\u5802\u4e13\u6ce8\u8868\u73b0{focus}\uff0c\u5bf9\u672c\u8282\u5185\u5bb9{mastery}\u3002"
    parts = [opening, state]
    if problems:
        parts.append(f"\u76ee\u524d\u9700\u8981\u7ee7\u7eed\u52a0\u5f3a\uff1a{problems}\u3002")
    if homework:
        parts.append(f"\u8bfe\u540e\u5b89\u6392\uff1a{homework}\u3002")
    if next_plan:
        parts.append(f"\u4e0b\u4e00\u6b65\u5c06\u91cd\u70b9\u8fdb\u884c\uff1a{next_plan}\u3002")
    if notes:
        parts.append(notes if notes.endswith(("\u3002", "\uff01", "\uff1f")) else notes + "\u3002")
    return "".join(parts)

def _ai_polish(template_text, data, get_cfg):
    api_key = _clean(get_cfg("ai_api_key"))
    if not api_key:
        raise ValueError("\u672a\u914d\u7f6e AI API Key")
    api_url = _clean(get_cfg("ai_api_url")) or "https://api.deepseek.com/v1/chat/completions"
    model = _clean(get_cfg("ai_model")) or "deepseek-chat"
    kind_label = "\u6297\u9057\u5fd8\u590d\u4e60" if data.get("feedback_kind") == "review" else "\u4e3b\u8bfe"
    prompt = f"""\u4f60\u662f\u4e13\u4e1a\u7684\u4e00\u5bf9\u4e00\u6559\u5b66\u53cd\u9988\u52a9\u624b\u3002
\u8bf7\u628a\u4e0b\u65b9\u8349\u7a3f\u6da6\u8272\u6210\u9002\u5408\u76f4\u63a5\u53d1\u7ed9\u5bb6\u957f\u7684\u4e2d\u6587\u53cd\u9988\u3002
\u8981\u6c42\uff1a
1. \u53ea\u80fd\u4f7f\u7528\u8349\u7a3f\u548c\u8f93\u5165\u4e2d\u660e\u786e\u63d0\u4f9b\u7684\u4fe1\u606f\uff0c\u4e25\u7981\u7f16\u9020\u5b66\u751f\u8868\u73b0\u3002
2. \u8bed\u6c14\u4e13\u4e1a\u3001\u5177\u4f53\u3001\u6e29\u548c\uff0c\u5148\u80af\u5b9a\u518d\u8bf4\u9700\u8981\u6539\u8fdb\u7684\u5185\u5bb9\u3002
3. 120-260\u4e2a\u4e2d\u6587\u5b57\uff0c\u4e0d\u4f7f\u7528 Markdown\u6807\u9898\uff0c\u4e0d\u52a0\u201cAI\u751f\u6210\u201d\u7b49\u5b57\u6837\u3002
4. \u5982\u679c\u662f\u6297\u9057\u5fd8\u590d\u4e60\uff0c\u8981\u7a81\u51fa\u8bb0\u5fc6\u4fdd\u6301\u3001\u6613\u9519\u70b9\u548c\u4e0b\u6b21\u590d\u4e60\u8ba1\u5212\u3002

\u53cd\u9988\u7c7b\u578b\uff1a{kind_label}
\u8349\u7a3f\uff1a{template_text}
\u53ea\u8f93\u51fa\u6700\u7ec8\u53cd\u9988\u6b63\u6587\u3002"""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "\u4f60\u53ea\u8d1f\u8d23\u6da6\u8272\u771f\u5b9e\u7684\u6559\u5b66\u53cd\u9988\uff0c\u4e0d\u5f97\u7f16\u9020\u4efb\u4f55\u4e8b\u5b9e\u3002"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
        "max_tokens": 500,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(api_url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    with urllib.request.urlopen(req, timeout=25) as response:
        result = json.loads(response.read().decode("utf-8"))
    text = _clean(result.get("choices", [{}])[0].get("message", {}).get("content"))
    if not text:
        raise ValueError("AI \u672a\u8fd4\u56de\u53cd\u9988\u5185\u5bb9")
    return text

def generate_feedback(data, get_cfg, use_ai=False):
    template = build_template_feedback(data)
    if not use_ai:
        return {"text": template, "method": "template", "warning": ""}
    try:
        text = _ai_polish(template, data, get_cfg)
        return {"text": text, "method": "ai", "warning": ""}
    except Exception as exc:
        return {"text": template, "method": "template", "warning": f"AI\u6da6\u8272\u672a\u5b8c\u6210\uff0c\u5df2\u4f7f\u7528\u514d\u8d39\u6a21\u677f\uff1a{exc}"}

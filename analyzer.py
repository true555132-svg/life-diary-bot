"""Claude 分析層 - 把生活紀錄轉成摘要與建議"""
import os, json, re, base64, urllib.request
import anthropic

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "claude-sonnet-4-6")

TAG_OPTIONS = ["心情", "工作", "健康", "人際", "學習", "休閒", "其他"]

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def classify_entry(text: str) -> tuple:
    """回傳 (tags: list[str], mood_score: int|None)。用於文字紀錄與圖片描述。"""
    if not _client or not text.strip():
        return [], None
    prompt = (
        f"以下是一筆生活紀錄文字：「{text}」\n\n"
        f"請從這些標籤中選 1-2 個最符合的：{TAG_OPTIONS}\n"
        "並評估這筆紀錄透露的心情分數（1=很差，5=很好；如果完全看不出心情就填 null）。\n"
        '只回傳 JSON，格式：{"tags": ["..."], "mood_score": 數字或null}，不要其他文字。'
    )
    try:
        resp = _client.messages.create(
            model=ANALYSIS_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _extract_json(resp.content[0].text)
        tags = [t for t in data.get("tags", []) if t in TAG_OPTIONS]
        mood = data.get("mood_score")
        mood = int(mood) if isinstance(mood, (int, float)) else None
        return tags, mood
    except Exception as e:
        print(f"[Analyzer] classify_entry error: {e}")
        return [], None


def describe_image(image_url: str, caption: str = "") -> str:
    """用 Claude vision 幫一張剛上傳的圖片產生簡短描述，存進 content 方便之後分析。"""
    if not _client:
        return caption
    try:
        with urllib.request.urlopen(image_url, timeout=15) as r:
            data = r.read()
        b64 = base64.b64encode(data).decode()
        resp = _client.messages.create(
            model=ANALYSIS_MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": f"這是使用者生活紀錄上傳的一張照片。用一句話描述照片內容（中文，不要客套話）。"
                                              f"{f'使用者備註：{caption}' if caption else ''}"},
                ],
            }],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[Analyzer] describe_image error: {e}")
        return caption


def _format_entries(entries: list) -> str:
    lines = []
    for e in entries:
        tag = "📷" if e["entry_type"] == "image" else "📝"
        meta = []
        if e.get("tags"):
            meta.append("/".join(e["tags"]))
        if e.get("mood_score") is not None:
            meta.append(f"心情{e['mood_score']}/5")
        meta_str = f" ({', '.join(meta)})" if meta else ""
        lines.append(f"{tag} [{e['created_at']}]{meta_str} {e['content']}")
    return "\n".join(lines) if lines else "(這段時間沒有紀錄)"


def analyze(entries: list, period_label: str) -> str:
    """產生摘要 + 建議。entries 為 db.get_entries() 回傳的 list。"""
    if not entries:
        return f"{period_label}還沒有任何紀錄，先記點什麼吧～"
    if not _client:
        return f"{period_label}共有 {len(entries)} 筆紀錄（未設定 ANTHROPIC_API_KEY，無法產生 AI 分析）。"
    text_block = _format_entries(entries)
    moods = [e["mood_score"] for e in entries if e.get("mood_score") is not None]
    mood_line = f"\n心情分數序列：{moods}（平均 {sum(moods)/len(moods):.1f}）\n" if moods else ""
    prompt = (
        f"以下是使用者在「{period_label}」記錄的生活筆記（含照片描述、AI標籤、心情分數）：\n\n{text_block}\n{mood_line}\n"
        "請用繁體中文簡短回覆，包含：\n"
        "1. 一段摘要這段時間發生的事/狀態\n"
        "2. 觀察到的模式或情緒/生活習慣傾向（如果看得出來，包含心情走勢）\n"
        "3. 2-3 條具體可執行的建議\n"
        "語氣像朋友聊天，不要說教，不要用條列符號以外的格式（不要markdown標題）。"
    )
    try:
        resp = _client.messages.create(
            model=ANALYSIS_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[Analyzer] analyze error: {e}")
        return f"{period_label}共有 {len(entries)} 筆紀錄，但分析時發生錯誤，稍後再試。"

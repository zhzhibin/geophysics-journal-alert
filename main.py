import os
import re
import json
import html
import smtplib
import requests
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.oauth2 import service_account
from googleapiclient.discovery import build


# ===== 你要追踪的期刊 =====
# 这里用的是常见电子版 ISSN。
JOURNALS = {
    "Geophysical Research Letters": "1944-8007",
    "JGR Solid Earth": "2169-9356",
    "JGR Atmospheres": "2169-8996",
    "JGR Oceans": "2169-9291",
    "JGR Planets": "2169-9100",
    "JGR Space Physics": "2169-9402",
    "Tectonics": "1944-9194",
    "Reviews of Geophysics": "1944-9208",
}

# 每次抓取过去几天的新文章
DAYS_BACK = 3

# DOI 去重文件
SEEN_FILE = "seen_dois.json"


def clean_text(text):
    """清理 HTML、空格和特殊字符。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def generate_chinese_summary(text):
    """
    当前版本：不调用付费 AI，只把英文摘要保留到中文摘要栏。
    后续如果你要接 OpenAI / DeepL，可以替换这个函数。
    """
    if not text:
        return "无摘要。"
    return f"英文摘要原文：{text}"


def load_seen_dois():
    """读取已经处理过的 DOI。"""
    if not os.path.exists(SEEN_FILE):
        return set()

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception:
        return set()


def save_seen_dois(seen):
    """保存已经处理过的 DOI。"""
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, ensure_ascii=False, indent=2)


def get_published_date(item):
    """从 Crossref 数据中提取发布日期。"""
    for key in ["published-online", "published-print", "published"]:
        if key in item:
            parts = item[key].get("date-parts", [[]])[0]
            if parts:
                year = str(parts[0])
                month = str(parts[1]).zfill(2) if len(parts) > 1 else "01"
                day = str(parts[2]).zfill(2) if len(parts) > 2 else "01"
                return f"{year}-{month}-{day}"
    return ""


def fetch_crossref_articles(journal_name, issn):
    """从 Crossref 按 ISSN 抓取过去 DAYS_BACK 天的新文章。"""
    from_date = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).date().isoformat()

    url = f"https://api.crossref.org/journals/{issn}/works"
    params = {
        "filter": f"from-pub-date:{from_date},type:journal-article",
        "select": "DOI,title,abstract,published-online,published-print,published,URL,container-title",
        "sort": "published",
        "order": "desc",
        "rows": 100,
        "mailto": os.getenv("EMAIL_USER", ""),
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()

    items = response.json().get("message", {}).get("items", [])
    articles = []

    for item in items:
        doi = item.get("DOI", "").strip()
        if not doi:
            continue

        title_list = item.get("title", [])
        title = clean_text(title_list[0]) if title_list else ""

        abstract_en = clean_text(item.get("abstract", ""))
        abstract_zh = generate_chinese_summary(abstract_en)

        article_url = item.get("URL", "")
        published = get_published_date(item)

        articles.append({
            "journal": journal_name,
            "published": published,
            "title": title,
            "abstract_zh": abstract_zh,
            "abstract_en": abstract_en,
            "doi": doi,
            "url": article_url,
        })

    return articles


def build_email_html(articles):
    """生成 HTML 邮件正文。"""
    today = datetime.now().strftime("%Y-%m-%d")

    if not articles:
        return f"""
        <h2>地球物理期刊新文章简报｜{today}</h2>
        <p>过去 {DAYS_BACK} 天没有抓取到新的未读文章。</p>
        """

    parts = [
        f"<h2>地球物理期刊新文章简报｜{today}</h2>",
        f"<p>过去 {DAYS_BACK} 天共抓取到 <b>{len(articles)}</b> 篇新文章。</p>"
    ]

    for idx, article in enumerate(articles, 1):
        parts.append(f"""
        <hr>
        <h3>{idx}. {article['title']}</h3>
        <p><b>期刊：</b>{article['journal']}</p>
        <p><b>发布日期：</b>{article['published']}</p>
        <p><b>DOI：</b>{article['doi']}</p>
        <p><b>链接：</b><a href="{article['url']}">{article['url']}</a></p>
        <p><b>中文摘要：</b>{article['abstract_zh']}</p>
        <p><b>英文摘要：</b>{article['abstract_en']}</p>
        """)

    return "\n".join(parts)


def send_email(subject, html_body):
    """用 Gmail SMTP 发邮件。支持 EMAIL_TO 里填多个邮箱，用英文逗号分隔。"""
    email_user = os.environ["EMAIL_USER"]
    email_pass = os.environ["EMAIL_PASS"]

    email_to_list = [
        x.strip()
        for x in os.environ["EMAIL_TO"].split(",")
        if x.strip()
    ]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_user
    msg["To"] = ", ".join(email_to_list)

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(email_user, email_pass)
        server.sendmail(email_user, email_to_list, msg.as_string())


def append_to_google_sheet(articles):
    """把新文章追加写入 Google Sheet。"""
    if not articles:
        return

    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    service_account_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes
    )

    service = build("sheets", "v4", credentials=credentials)

    today = datetime.now().strftime("%Y-%m-%d")
    rows = []

    for article in articles:
        rows.append([
            today,
            article["journal"],
            article["published"],
            article["title"],
            article["abstract_zh"],
            article["abstract_en"],
            article["doi"],
            article["url"],
        ])

    body = {"values": rows}

    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range="Sheet1!A:H",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body
    ).execute()


def main():
    seen = load_seen_dois()
    new_articles = []

    for journal_name, issn in JOURNALS.items():
        print(f"Fetching {journal_name}...")
        try:
            articles = fetch_crossref_articles(journal_name, issn)

            for article in articles:
                doi_key = article["doi"].lower()
                if doi_key not in seen:
                    new_articles.append(article)
                    seen.add(doi_key)

        except Exception as e:
            print(f"Error fetching {journal_name}: {e}")

    new_articles.sort(
        key=lambda x: (x["journal"], x["published"], x["title"])
    )

    subject = f"地球物理期刊新文章简报｜新增 {len(new_articles)} 篇"
    html_body = build_email_html(new_articles)

    append_to_google_sheet(new_articles)
    send_email(subject, html_body)
    save_seen_dois(seen)

    print(f"Done. New articles: {len(new_articles)}")


if __name__ == "__main__":
    main()

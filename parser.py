import html
import json
import re
import sys
import xml.etree.ElementTree as ET

MAX_ANSWERS=3
MIN_WORD_COUNT=30
SNIPPET_CHARS = 200 #for search results later 


HTMLTAGS_RE = re.compile(r"<[^>]+>") #tag regex
WS_RE = re.compile(r"\s+") #whitespace regex
TAGS_RE = re.compile(r"<([^>]+)>") #tags regex
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
A_HREF_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|bmp|svg)(?:[?#]\S*)?$", re.IGNORECASE)
IMG_HOSTS = ("i.sstatic.net", "imgur.com")
URL_TRAIL = ".,;:!?)]}'\""
#used this compiled functions instead of regular strings so it can be reused quickly without recompiling it every time the function runs

#removing html tags
def clean_text(text):
    if not text:
        return ""
    text=html.unescape(text)
    text=HTMLTAGS_RE.sub(" ", text)
    text=URL_RE.sub(" ", text)      # drop URLs, replace with a space
    text=WS_RE.sub(" ", text).strip()
    return text

def extract_media(raw_html):
    """
    Pull URLs from the RAW body (before cleaning) for display only.
    Returns (images, links): deduplicated, in order of appearance.
    Only http(s) URLs are kept.
    """
    if not raw_html:
        return [], []
    urls = [html.unescape(u) for u in IMG_SRC_RE.findall(raw_html)]
    urls += [html.unescape(u) for u in A_HREF_RE.findall(raw_html)]
    bare_text = html.unescape(HTMLTAGS_RE.sub(" ", raw_html))
    urls += [u.rstrip(URL_TRAIL) for u in URL_RE.findall(bare_text)]

    images, links = [], []
    for u in dict.fromkeys(u.strip() for u in urls):
        if u.lower().startswith("www."):
            u = "http://" + u
        if not u.lower().startswith(("http://", "https://")):
            continue
        is_img = IMG_EXT_RE.search(u) or any(h in u.lower() for h in IMG_HOSTS)
        (images if is_img else links).append(u)
    return images, links


def to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
    
#converting tags to list of strings
def parse_tags(raw):
    return TAGS_RE.findall(raw) if raw else []

#converting timestamp to date and year 
def parse_date(raw):
    
    date = (raw or "")[:10] #string slicing to get first 10 characters which is yyyy-mm-dd
    return date, to_int(date[:4], 0) #return date and year tuple


def iter_rows(path):
    #used iterparse the built in fn to parse xml,reads line by line,yields ,discards
    context = ET.iterparse(path, events=("start", "end"))
    _, root = next(context)  # first event is the start of <posts>
    for event, elem in context: #loop through the events and elements in the context
        if event == "end" and elem.tag == "row": 
            yield elem
            elem.clear()
            root.clear()  # also drop the root's references to cleared rows

def collect_answers(path):
    answers = {}
    total = 0
    for row in iter_rows(path):
        a = row.attrib
        if a.get("PostTypeId") != "2":
            continue
        parent = to_int(a.get("ParentId"), None)
        text = clean_text(a.get("Body"))
        images, links = extract_media(a.get("Body"))  # display only
        if parent is None or not (text or images):
            continue
        total += 1
        bucket = answers.setdefault(parent, [])
        bucket.append({"text": text, "score": to_int(a.get("Score")),"images": images, "links": links})
        if len(bucket) > MAX_ANSWERS:  #if more than 3 answers, sort by score and keep only the top 3
            bucket.sort(key=lambda x: x["score"], reverse=True)
            del bucket[MAX_ANSWERS:]
    print(f"Pass 1: read {total:,} answers for {len(answers):,} questions")
    return answers

def build_questions(path, answers, jsonl_path, info_path):
    doc_info = {}
    doc_id = 0
    dropped = 0

    # Binary mode so tell() gives exact byte offsets
    with open(jsonl_path, "wb") as out:
        for row in iter_rows(path):
            a = row.attrib
            if a.get("PostTypeId") != "1":
                continue

            se_id = to_int(a.get("Id"), None)
            if se_id is None:
                continue

            # Pop first so memory is freed even for dropped questions.
            top = answers.pop(se_id, [])
            top.sort(key=lambda x: x["score"], reverse=True)

            title = clean_text(a.get("Title"))
            body = clean_text(a.get("Body"))
            if len((title + " " + body).split()) < MIN_WORD_COUNT: #concatenate title and body,split into words, check if less than 30 words
                dropped += 1
                continue

            date, year = parse_date(a.get("CreationDate"))
            images, links = extract_media(a.get("Body"))  # display only
            record = {
                "doc_id": doc_id,
                "se_id": se_id,
                "title": title,
                "body": body,
                "tags": parse_tags(a.get("Tags")),
                "score": to_int(a.get("Score")),
                "views": to_int(a.get("ViewCount")),
                "answer_count": to_int(a.get("AnswerCount")),
                "date": date,
                "year": year,
                "answers": top,
                "images": images,
                "links": links,
            }

            offset = out.tell() #before writing the record, get the current byte offset in the file(will be used for fast access later)
            out.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))

            doc_info[str(doc_id)] = {
                "se_id": se_id,
                "title": title,
                "tags": record["tags"],
                "score": record["score"],
                "views": record["views"],
                "answer_count": record["answer_count"],
                "date": date,
                "year": year,
                "snippet": body[:SNIPPET_CHARS],
                "offset": offset,
            }
            doc_id += 1

    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(doc_info, f, ensure_ascii=False)

    print(f"Pass 2: wrote {doc_id:,} questions, dropped {dropped:,} "
          f"(< {MIN_WORD_COUNT} words)")

def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "Posts.xml"
    jsonl_path = sys.argv[2] if len(sys.argv) > 2 else "questions.jsonl"
    info_path = sys.argv[3] if len(sys.argv) > 3 else "doc_info.json"

    answers = collect_answers(src)
    build_questions(src, answers, jsonl_path, info_path)
    print(f"Outputs: {jsonl_path}, {info_path}")


if __name__ == "__main__":
    main()
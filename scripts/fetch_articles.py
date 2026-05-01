#!/usr/bin/env python3
"""
SV Update — fetch_articles.py
Fetches Sinus/Voice/ENT articles from PubMed, analyzes with Claude AI,
saves JSON data files, and sends Telegram notification.
"""

import os, sys, json, time, requests, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
import html as html_module

# ─── Configuration ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "1276595563")
EMAIL_FOR_UNPAYWALL = "sv-update@gmail.com"
SITE_URL            = "https://malbarr.github.io/sv-update"

MAX_ARTICLES        = 25
KEEP_DAYS           = 60
CLAUDE_MODEL        = "claude-haiku-4-5-20251001"

# Quality thresholds (per user specifications)
MIN_STARS_PRIORITY  = 3   # sinus/voice/skull_base: keep ≥ 3 stars
MIN_STARS_GENERAL   = 4   # all others: keep ≥ 4 stars
PRIORITY_SUBSPECIALTIES = {'rhinology', 'skull_base', 'laryngology'}

PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
UNPAYWALL_URL     = "https://api.unpaywall.org/v2/{doi}?email=" + EMAIL_FOR_UNPAYWALL

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# Business news RSS
BUSINESS_RSS = (
    "https://news.google.com/rss/search?"
    "q=ENT+sinus+voice+otolaryngology+medical+device+Medtronic+Stryker"
    "&hl=en&gl=US&ceid=US:en"
)
MAX_BUSINESS_ARTICLES = 3

DATA_DIR  = Path(__file__).parent.parent / "data"
AUDIO_DIR = Path(__file__).parent.parent / "audio"
DATA_DIR.mkdir(exist_ok=True)
AUDIO_DIR.mkdir(exist_ok=True)

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ─── PubMed Queries ─────────────────────────────────────────────────────────────

# Priority: Sinus + Voice — 2 days, higher recall
PRIORITY_QUERY = (
    "(rhinology[tiab] OR \"nasal polyp\"[tiab] OR \"chronic rhinosinusitis\"[tiab] OR "
    "sinusitis[MeSH] OR \"nasal polyps\"[MeSH] OR septoplasty[tiab] OR "
    "\"endoscopic sinus surgery\"[tiab] OR FESS[tiab] OR \"turbinate\"[tiab] OR "
    "\"skull base\"[MeSH] OR \"skull base surgery\"[tiab] OR "
    "\"endoscopic skull base\"[tiab] OR \"pituitary surgery\"[tiab] OR "
    "\"CSF leak\"[tiab] OR \"cerebrospinal fluid leak\"[tiab] OR "
    "\"sinonasal\"[tiab] OR \"olfactory\"[tiab] OR \"smell disorder\"[tiab] OR "
    "laryngology[tiab] OR \"vocal fold\"[tiab] OR \"vocal cord\"[MeSH] OR "
    "larynx[MeSH] OR \"laryngeal cancer\"[tiab] OR \"subglottic\"[tiab] OR "
    "\"supraglottic\"[tiab] OR \"phonosurgery\"[tiab] OR \"voice disorder\"[tiab] OR "
    "\"laryngotracheal\"[tiab] OR \"spasmodic dysphonia\"[tiab] OR "
    "\"laryngopharyngeal reflux\"[tiab] OR LPR[tiab] OR \"dysphagia\"[tiab] OR "
    "\"sleep apnea\"[MeSH] OR \"obstructive sleep apnea\"[tiab] OR OSA[tiab])"
)

# General ENT — 1 day
ENT_QUERY = (
    "(otolaryngology[MeSH] OR \"otorhinolaryngologic diseases\"[MeSH] OR "
    "sinusitis[MeSH] OR \"nasal polyps\"[MeSH] OR \"cochlear implants\"[MeSH] OR "
    "tonsillectomy[MeSH] OR \"vocal cords\"[MeSH] OR larynx[MeSH] OR "
    "\"otitis media\"[MeSH] OR \"head and neck neoplasms\"[MeSH] OR "
    "\"sleep apnea, obstructive\"[MeSH] OR rhinoplasty[MeSH] OR "
    "\"skull base\"[MeSH] OR mastoidectomy[MeSH] OR stapedectomy[MeSH] OR "
    "tympanoplasty[tiab] OR septoplasty[tiab] OR FESS[tiab] OR "
    "\"endoscopic sinus surgery\"[tiab] OR \"skull base surgery\"[tiab] OR "
    "rhinology[tiab] OR otology[tiab] OR laryngology[tiab] OR "
    "\"vocal fold\"[tiab] OR \"subglottic\"[tiab] OR \"nasal polyp\"[tiab] OR "
    "\"laryngeal cancer\"[tiab] OR \"cochlear implant\"[tiab] OR "
    "\"obstructive sleep apnea\"[tiab] OR \"uvulopalatopharyngoplasty\"[tiab])"
)

# ─── PubMed helpers ─────────────────────────────────────────────────────────────

def _pubmed_search(query, reldate, retmax):
    # Quality filter: RCT, Meta-analysis, Systematic Review, Cohort, Guideline
    quality_filter = (
        " AND ("
        "\"randomized controlled trial\"[pt] OR \"meta-analysis\"[pt] OR "
        "\"systematic review\"[pt] OR \"practice guideline\"[pt] OR "
        "\"clinical trial\"[pt] OR \"cohort study\"[tiab] OR "
        "\"comparative study\"[pt]"
        ")"
        " AND English[Language]"
        " AND hasabstract[text]"
        " AND medline[sb]"
    )
    try:
        r = requests.get(PUBMED_SEARCH_URL, params={
            "db": "pubmed", "term": query + quality_filter,
            "datetype": "pdat", "reldate": str(reldate),
            "retmax": str(retmax), "sort": "relevance", "retmode": "json",
        }, timeout=30)
        r.raise_for_status()
        return r.json().get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        print(f"[PubMed] Search error: {e}")
        return []


def search_pubmed():
    priority_pmids = _pubmed_search(PRIORITY_QUERY, reldate=2, retmax=15)
    general_pmids  = _pubmed_search(ENT_QUERY,      reldate=1, retmax=MAX_ARTICLES)
    seen, pmids = set(), []
    for p in priority_pmids + general_pmids:
        if p not in seen:
            seen.add(p); pmids.append(p)
    print(f"[PubMed] Priority: {len(priority_pmids)}, General: {len(general_pmids)}, Unique: {len(pmids)}")
    return pmids


def fetch_pubmed_xml(pmids):
    try:
        r = requests.get(PUBMED_FETCH_URL, params={
            "db": "pubmed", "id": ",".join(pmids), "retmode": "xml"
        }, timeout=60)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[PubMed] Fetch error: {e}"); return ""


def _text_node(el, path, default=""):
    node = el.find(path)
    return (node.text or "").strip() if node is not None else default

def _iter_text(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_pubmed_xml(xml_text):
    if not xml_text: return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[XML] Parse error: {e}"); return []

    articles = []
    for pub_article in root.findall(".//PubmedArticle"):
        try:
            medline = pub_article.find(".//MedlineCitation")
            art = medline.find("Article") if medline is not None else None
            if art is None: continue

            pmid    = _text_node(medline, "PMID")
            title   = _iter_text(art.find("ArticleTitle"))
            journal = _text_node(art, "Journal/Title")

            abstract_parts = []
            for ab in art.findall(".//AbstractText"):
                label = ab.get("Label", "")
                text  = _iter_text(ab)
                if label: abstract_parts.append(f"{label}: {text}")
                elif text: abstract_parts.append(text)
            abstract = " ".join(abstract_parts)

            pub_date_el = art.find(".//PubDate")
            if pub_date_el is not None:
                year  = _text_node(pub_date_el, "Year", "")
                month = _text_node(pub_date_el, "Month", "")
                day   = _text_node(pub_date_el, "Day", "")
                pub_date = "-".join(p for p in [year, month, day] if p)
            else:
                pub_date = TODAY

            # DOI
            doi = ""
            for loc in art.findall(".//ELocationID"):
                if loc.get("EIdType") == "doi":
                    doi = (loc.text or "").strip(); break
            if not doi:
                for aid in pub_article.findall(".//ArticleId"):
                    if aid.get("IdType") == "doi":
                        doi = (aid.text or "").strip(); break

            # Publication types
            pub_types = [_iter_text(pt) for pt in medline.findall(".//PublicationType")]

            articles.append({
                "pmid": pmid, "title": title, "journal": journal,
                "abstract": abstract, "pub_date": pub_date, "doi": doi,
                "pub_types": pub_types,
            })
        except Exception as e:
            print(f"[XML] Article parse error: {e}"); continue

    return articles


# ─── Unpaywall ──────────────────────────────────────────────────────────────────

def get_free_pdf(doi):
    if not doi: return None
    try:
        r = requests.get(UNPAYWALL_URL.format(doi=doi), timeout=15)
        if r.status_code == 200:
            data = r.json()
            oa_loc = data.get("best_oa_location")
            if oa_loc:
                return oa_loc.get("url_for_pdf") or oa_loc.get("url")
    except Exception: pass
    return None


# ─── Claude Analysis ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a medical editor for SV Update, a daily ENT literature review. "
    "You specialize in Sinus/Rhinology, Skull Base, and Voice/Laryngology. "
    "Analyze articles and return structured JSON for clinicians. "
    "Return ONLY valid JSON — no markdown fences, no explanation."
)

ANALYSIS_PROMPT = """Analyze this ENT article and return ONLY a valid JSON object (no markdown fences).

Title: {title}
Journal: {journal}
Abstract: {abstract}

QUALITY SCORING RULES (apply strictly):

Study Design — base score:
- RCT or Meta-analysis/Systematic Review: strong base (+2) → eligible for stars 4-5
- Prospective cohort ≥100: good (+1)
- Retrospective series >100: acceptable (0)
- Case series <20: weak (-1)
- Case report: REJECT unless truly novel technique/rare condition
- Editorial/Expert opinion: REJECT unless paradigm-shifting

Journal Quality:
- NEJM, JAMA, Lancet, BMJ, JAMA Otolaryngol, Laryngoscope, Otolaryngol HNS, Head & Neck, Rhinology, Rhinology Online, Int Forum Allergy Rhinol, J Voice, Laryngoscope Investigative Otolaryngol: tier 1 (+1)
- Unknown or predatory journal: -1 or REJECT

Clinical Impact (primary factor):
- Practice-changing (changes how you operate/manage today): confidence 🟢 → stars 5
- Worth knowing (adds knowledge, does not change practice yet): confidence 🟡 → stars 3-4
- Confirms known / weak evidence: confidence 🔴 → stars 1-2, consider reject

Novelty:
- New technique, new drug application, unexpected finding: +1
- Confirms prior knowledge without adding value: -1

IMMEDIATE REJECTION (reject=true, stars=1):
- ENT mentioned only incidentally; primary topic is another specialty
- No results or conclusions in abstract
- Animal or in vitro study
- n < 10 patients
- Published before 2020 UNLESS landmark/classic
- No clinical relevance

PRIORITY SUBSPECIALTY EXCEPTIONS (rhinology, skull_base, laryngology) — do NOT reject:
- Case series ≥10 with genuinely new technique
- Retrospective n > 100

SCORING SYSTEM (0-100):
- Study design (30 pts): RCT/Meta = 30, Prospective cohort = 20, Retrospective = 10
- Journal quality (25 pts): Tier 1 = 25, Tier 2 = 15, Unknown = 5
- Sample size (20 pts): ≥500 = 20, 100-499 = 15, 50-99 = 10, 10-49 = 5
- Clinical relevance (15 pts): practice-changing = 15, worth knowing = 10, weak = 3
- Novelty (10 pts): new = 10, confirms = 5, no novelty = 0
Score ≥ 70: publish. Priority subspecialties: score ≥ 55.

JSON fields required:
- title_en: original English title
- title_ar: IDENTICAL to title_en (do NOT translate)
- study_design: brief descriptor e.g. "RCT n=240", "Meta-analysis 18 studies", "Prospective cohort n=312"
- summary_ar: 5-7 sentence Arabic summary. CRITICAL: ALL medical/anatomical/statistical terms stay in English (Latin characters). Only Arabic connectors/verbs. Examples: "meta-analysis", "RCT", "sinusitis", "vocal fold", "FESS", "p-value", "hazard ratio", "dupilumab", "CPAP", "laryngoscopy".
- summary_en: 5-7 sentence English summary
- practice_change_ar: one sentence — what changes in clinical practice today (Arabic, medical terms in English)
- practice_change_en: same in English
- future_impact_ar: one sentence — future implications (Arabic)
- future_impact_en: same in English
- why_important_ar: one sentence — why this matters (Arabic)
- why_important_en: same in English
- vs_previous_ar: one sentence — how it differs from previous guidelines/knowledge (Arabic)
- vs_previous_en: same in English
- stars: integer 1-5 (apply scoring rules)
- stars_reason_ar: brief Arabic reason for score (medical terms in English)
- stars_reason_en: same in English
- confidence: "🟢" or "🟡" or "🔴"
- reject: boolean
- reject_reason: brief English reason if reject=true, else null
- journal_club: boolean (true if worth presenting at journal club — stars ≥ 4 AND high clinical impact)
- jc_reason_ar: reason if journal_club=true, else null
- jc_reason_en: same in English, or null
- watch: boolean (true if features noteworthy drug/device/instrument/technology)
- watch_detail_ar: one sentence describing what to watch, or null
- watch_detail_en: same in English, or null
- watch_type: "drug" | "device" | "technology" | "instrument" | null
- research_gap_ar: one sentence — key research gap or unanswered question, or null
- research_gap_en: same in English, or null
- subspecialty: one of: rhinology, skull_base, laryngology, otology, head_neck, pediatric, sleep, general
- audio_script_ar: 2-3 minute Arabic audio script (medical terms in English) covering: study type, key findings, clinical impact, research gap, 1 MCQ
- audio_script_en: 2-3 minute English audio script covering same points
- mcq: array of exactly 3 objects, each with: q_ar, q_en, options_ar (array 4 strings prefixed أ) ب) ج) د)), options_en (array 4 strings prefixed A) B) C) D)), answer (0-3 index), explanation_ar, explanation_en
"""

BUSINESS_SYSTEM_PROMPT = (
    "You are a business editor for SV Update, an ENT industry news digest. "
    "Analyze ENT industry news (devices, FDA decisions, acquisitions, research funding) "
    "and return structured JSON for ENT surgeons. Return ONLY valid JSON."
)

BUSINESS_ANALYSIS_PROMPT = """Analyze this ENT industry/business news and return ONLY a valid JSON object.

Title: {title}
Description: {description}

JSON fields required:
- title_en: cleaned English headline
- title_ar: Arabic translation of headline
- study_design: "Industry News"
- summary_en: 3-5 sentences — what happened and relevance to ENT surgeons
- summary_ar: same in Arabic (medical/device terms stay in English)
- practice_change_en: one sentence — why this matters to practicing ENT surgeons
- practice_change_ar: same in Arabic
- why_important_en: one sentence — clinical or practice impact
- why_important_ar: same in Arabic
- future_impact_en: one sentence — what this means for ENT going forward
- future_impact_ar: same in Arabic
- stars: integer 1-5 (importance to ENT surgeons)
- stars_reason_en: brief reason
- stars_reason_ar: same in Arabic
- confidence: "🟡"
- reject: false
- reject_reason: null
- journal_club: false
- jc_reason_en: null
- jc_reason_ar: null
- watch: true
- watch_detail_en: one sentence describing the device/drug/company to watch
- watch_detail_ar: same in Arabic
- watch_type: "device" or "technology" or "drug"
- research_gap_en: null
- research_gap_ar: null
- subspecialty: "business"
- audio_script_ar: 1-2 minute Arabic audio script about the news
- audio_script_en: 1-2 minute English audio script
- mcq: []
"""


def _strip_fences(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return raw.strip()


def _call_claude(system, user_content):
    """Call Claude API and return response text."""
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set")

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }
    r = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=120)
    r.raise_for_status()
    return r.json()["content"][0]["text"]


def analyze_article(article):
    prompt = ANALYSIS_PROMPT.format(
        title    = article["title"],
        journal  = article["journal"],
        abstract = article["abstract"] or "(no abstract available)",
    )
    raw = _strip_fences(_call_claude(SYSTEM_PROMPT, prompt))
    return json.loads(raw)


def analyze_business(item):
    prompt = BUSINESS_ANALYSIS_PROMPT.format(
        title       = item["title"],
        description = item.get("description", "(no description)"),
    )
    raw = _strip_fences(_call_claude(BUSINESS_SYSTEM_PROMPT, prompt))
    return json.loads(raw)


# ─── Business news ───────────────────────────────────────────────────────────────

def fetch_business_news():
    try:
        r = requests.get(BUSINESS_RSS, timeout=30, headers={"User-Agent": "SV-Update/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        results = []
        for item in root.findall(".//item")[:MAX_BUSINESS_ARTICLES]:
            title_el = item.find("title")
            desc_el  = item.find("description")
            title = html_module.unescape((title_el.text or "").strip()) if title_el is not None else ""
            desc  = html_module.unescape((desc_el.text  or "").strip()) if desc_el  is not None else ""
            if title: results.append({"title": title, "description": desc})
        print(f"[Business RSS] Fetched {len(results)} items.")
        return results
    except Exception as e:
        print(f"[Business RSS] Error: {e}"); return []


# ─── Data management ─────────────────────────────────────────────────────────────

def load_index():
    index_file = DATA_DIR / "index.json"
    if index_file.exists():
        try:
            return json.loads(index_file.read_text(encoding="utf-8")).get("dates", [])
        except Exception: pass
    return []

def save_index(dates):
    (DATA_DIR / "index.json").write_text(
        json.dumps({"dates": sorted(set(dates), reverse=True)}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def prune_old_files(dates):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    kept = []
    for d in dates:
        if d >= cutoff:
            kept.append(d)
        else:
            f = DATA_DIR / f"{d}.json"
            if f.exists():
                f.unlink()
                print(f"[Prune] Deleted {f.name}")
    return kept

def load_today_pmids():
    today_file = DATA_DIR / f"{TODAY}.json"
    if today_file.exists():
        try:
            data = json.loads(today_file.read_text(encoding="utf-8"))
            return {a["pmid"] for a in data if a.get("pmid")}
        except Exception: pass
    return set()

def save_articles(articles, date=TODAY):
    out_file = DATA_DIR / f"{date}.json"
    # Load existing if present
    existing = []
    if out_file.exists():
        try:
            existing = json.loads(out_file.read_text(encoding="utf-8"))
        except Exception: pass
    # Merge, deduplicate by pmid
    seen_pmids = {a.get("pmid") for a in existing if a.get("pmid")}
    for a in articles:
        if a.get("pmid") not in seen_pmids:
            existing.append(a)
            seen_pmids.add(a.get("pmid"))
    out_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Save] {len(existing)} articles saved to {out_file.name}")
    return len(existing)


# ─── Telegram ────────────────────────────────────────────────────────────────────

def send_telegram(articles_saved, rejected, date=TODAY):
    if not TELEGRAM_BOT_TOKEN:
        print("[Telegram] No token — skipping."); return
    published = [a for a in articles_saved if not a.get("reject")]
    top = sorted(published, key=lambda x: x.get("stars", 0), reverse=True)[:3]
    lines = [f"📚 *SV Update — {date}*", f"✅ {len(published)} مقالة | ❌ {rejected} مرفوضة", ""]
    for a in top:
        conf = a.get("confidence", "🟡")
        stars_n = a.get("stars", 0)
        stars = "⭐" * stars_n + "☆" * (5 - stars_n)
        sub = a.get("subspecialty", "")
        title_short = (a.get("title_en", "") or "")[:60] + "..."
        lines.append(f"{conf} {stars} [{sub}]")
        lines.append(f"_{title_short}_")
        if a.get("practice_change_en"):
            lines.append(f"→ {a['practice_change_en'][:80]}")
        lines.append("")
    lines.append(f"[فتح SV Update]({SITE_URL})")
    msg = "\n".join(lines)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=15
        )
        print("[Telegram] Notification sent.")
    except Exception as e:
        print(f"[Telegram] Error: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"SV Update — {TODAY}")
    print(f"{'='*50}\n")

    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set"); sys.exit(1)

    # Load existing dates and today's already-processed PMIDs
    dates = load_index()
    existing_pmids = load_today_pmids()
    print(f"[Index] {len(dates)} dates loaded. Today: {len(existing_pmids)} articles already done.")

    # 1. Fetch PubMed articles
    pmids = search_pubmed()
    pmids = [p for p in pmids if p not in existing_pmids]
    print(f"[PubMed] {len(pmids)} new PMIDs to process.")

    results = []
    rejected_count = 0

    if pmids:
        xml_text = fetch_pubmed_xml(pmids)
        raw_articles = parse_pubmed_xml(xml_text)
        print(f"[Parse] {len(raw_articles)} articles parsed.")

        for art in raw_articles:
            print(f"  Analyzing: {art['title'][:60]}...")
            try:
                pdf_url = get_free_pdf(art["doi"])
                analyzed = analyze_article(art)
                time.sleep(0.5)  # Rate limiting

                # Determine if we keep or reject
                sub = analyzed.get("subspecialty", "general")
                stars = analyzed.get("stars", 1)
                reject = analyzed.get("reject", False)

                if not reject:
                    min_stars = MIN_STARS_PRIORITY if sub in PRIORITY_SUBSPECIALTIES else MIN_STARS_GENERAL
                    if stars < min_stars:
                        reject = True
                        analyzed["reject"] = True
                        analyzed["reject_reason"] = f"Stars {stars} < minimum {min_stars} for {sub}"

                if reject:
                    rejected_count += 1
                    print(f"    REJECTED: {analyzed.get('reject_reason', 'quality threshold')}")
                    continue

                # Build final record
                record = {
                    "pmid":             art["pmid"],
                    "title_en":         analyzed.get("title_en", art["title"]),
                    "title_ar":         analyzed.get("title_ar", art["title"]),
                    "journal":          art["journal"],
                    "pub_date":         art["pub_date"],
                    "doi":              art["doi"],
                    "pubmed_url":       f"https://pubmed.ncbi.nlm.nih.gov/{art['pmid']}/",
                    "pdf_url":          pdf_url,
                    "study_design":     analyzed.get("study_design", ""),
                    "subspecialty":     sub,
                    "stars":            stars,
                    "stars_reason_en":  analyzed.get("stars_reason_en", ""),
                    "stars_reason_ar":  analyzed.get("stars_reason_ar", ""),
                    "confidence":       analyzed.get("confidence", "🟡"),
                    "reject":           False,
                    "reject_reason":    None,
                    "summary_en":       analyzed.get("summary_en", ""),
                    "summary_ar":       analyzed.get("summary_ar", ""),
                    "practice_change_en": analyzed.get("practice_change_en", ""),
                    "practice_change_ar": analyzed.get("practice_change_ar", ""),
                    "future_impact_en": analyzed.get("future_impact_en", ""),
                    "future_impact_ar": analyzed.get("future_impact_ar", ""),
                    "why_important_en": analyzed.get("why_important_en", ""),
                    "why_important_ar": analyzed.get("why_important_ar", ""),
                    "vs_previous_en":   analyzed.get("vs_previous_en", ""),
                    "vs_previous_ar":   analyzed.get("vs_previous_ar", ""),
                    "research_gap_en":  analyzed.get("research_gap_en"),
                    "research_gap_ar":  analyzed.get("research_gap_ar"),
                    "journal_club":     analyzed.get("journal_club", False),
                    "jc_reason_en":     analyzed.get("jc_reason_en"),
                    "jc_reason_ar":     analyzed.get("jc_reason_ar"),
                    "watch":            analyzed.get("watch", False),
                    "watch_type":       analyzed.get("watch_type"),
                    "watch_detail_en":  analyzed.get("watch_detail_en"),
                    "watch_detail_ar":  analyzed.get("watch_detail_ar"),
                    "audio_script_en":  analyzed.get("audio_script_en", ""),
                    "audio_script_ar":  analyzed.get("audio_script_ar", ""),
                    "mcq":              analyzed.get("mcq", []),
                    "fetch_date":       TODAY,
                }
                results.append(record)
                print(f"    ✅ {stars}⭐ {analyzed.get('confidence','🟡')} [{sub}]")

            except json.JSONDecodeError as e:
                print(f"    JSON parse error: {e}")
            except Exception as e:
                print(f"    Error: {e}")

    # 2. Fetch business news
    biz_items = fetch_business_news()
    for item in biz_items:
        try:
            analyzed = analyze_business(item)
            time.sleep(0.5)
            record = {
                "pmid":             f"biz_{TODAY}_{hash(item['title']) % 10000:04d}",
                "title_en":         analyzed.get("title_en", item["title"]),
                "title_ar":         analyzed.get("title_ar", item["title"]),
                "journal":          "Industry News",
                "pub_date":         TODAY,
                "doi":              "",
                "pubmed_url":       "",
                "pdf_url":          None,
                "study_design":     "Industry News",
                "subspecialty":     "business",
                "stars":            analyzed.get("stars", 3),
                "stars_reason_en":  analyzed.get("stars_reason_en", ""),
                "stars_reason_ar":  analyzed.get("stars_reason_ar", ""),
                "confidence":       analyzed.get("confidence", "🟡"),
                "reject":           False,
                "reject_reason":    None,
                "summary_en":       analyzed.get("summary_en", ""),
                "summary_ar":       analyzed.get("summary_ar", ""),
                "practice_change_en": analyzed.get("practice_change_en", ""),
                "practice_change_ar": analyzed.get("practice_change_ar", ""),
                "future_impact_en": analyzed.get("future_impact_en", ""),
                "future_impact_ar": analyzed.get("future_impact_ar", ""),
                "why_important_en": analyzed.get("why_important_en", ""),
                "why_important_ar": analyzed.get("why_important_ar", ""),
                "vs_previous_en":   "",
                "vs_previous_ar":   "",
                "research_gap_en":  None,
                "research_gap_ar":  None,
                "journal_club":     False,
                "jc_reason_en":     None,
                "jc_reason_ar":     None,
                "watch":            True,
                "watch_type":       analyzed.get("watch_type", "device"),
                "watch_detail_en":  analyzed.get("watch_detail_en"),
                "watch_detail_ar":  analyzed.get("watch_detail_ar"),
                "audio_script_en":  analyzed.get("audio_script_en", ""),
                "audio_script_ar":  analyzed.get("audio_script_ar", ""),
                "mcq":              [],
                "fetch_date":       TODAY,
            }
            results.append(record)
            print(f"  📰 Business: {item['title'][:50]}...")
        except Exception as e:
            print(f"  Business error: {e}")

    # 3. Save
    if results:
        save_articles(results)
        if TODAY not in dates:
            dates.append(TODAY)
    dates = prune_old_files(dates)
    save_index(dates)

    print(f"\n[Done] Published: {len(results)}, Rejected: {rejected_count}")

    # 4. Telegram
    all_today = []
    today_file = DATA_DIR / f"{TODAY}.json"
    if today_file.exists():
        all_today = json.loads(today_file.read_text(encoding="utf-8"))
    send_telegram(all_today, rejected_count)


if __name__ == "__main__":
    main()

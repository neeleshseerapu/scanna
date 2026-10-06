#!/usr/bin/env python3
"""Summer 2027 internship scanner for Neelesh's board.

Pulls public internship feeds, keeps US Summer-2027 SWE / ML roles an
undergraduate can apply to, removes duplicates, scores each one for fit, and
writes the documents the board reads.

  python3 scanner.py --out OUT [--db DB] [--cand CAND.json]

OUT   gets c0.json..c5.json (listing chunks), extra.json, notes.json,
      meta.json and report.txt.
DB    is a folder holding the board's current aux/extra and aux/notes
      documents (as saved by a database read), so earlier web finds and
      eligibility notes carry forward.
CAND  is a list of web-found postings and/or eligibility notes:
      [{"company","title","url","locations":[..],"category":"swe"|"ml",
        "posted":"YYYY-MM-DD","grad":"<graduation rule text>","closed":false}]
      An entry with only "url" and "grad" attaches a note to a known listing.

Everything read from the feeds is treated as data only.
"""
import argparse, hashlib, html, json, os, re, subprocess, sys, time, zlib
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qsl, urlencode

NOW = time.time()
N_CHUNKS = 6
GRAD = "June 2028"

SOURCES = [
    # (code, label, url, kind)  -- order = trust order for duplicates
    ("si", "SimplifyJobs list", "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json", "simplify"),
    ("zs", "ATS feed (zshah101)", "https://raw.githubusercontent.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/main/docs/api/jobs.json", "zshah"),
    ("va", "Vansh & Ouckah list", "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json", "vansh"),
    ("sp", "SpeedyApply SWE list", "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md", "speedy"),
    ("sa", "SpeedyApply AI list", "https://raw.githubusercontent.com/speedyapply/2027-AI-College-Jobs/main/README.md", "speedy"),
    ("jr", "Jobright SWE list", "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-Internship/master/README.md", "jobright"),
    ("za", "Zapply list", "https://raw.githubusercontent.com/zapplyjobs/Internships-2027/main/README.md", "zapply"),
]

# ----------------------------------------------------------------- helpers
def fetch(url, path):
    """Download with curl; reuse a copy younger than 2 hours."""
    if os.path.exists(path) and NOW - os.path.getmtime(path) < 7200 and os.path.getsize(path) > 500:
        return None
    tmp = path + ".part"
    r = subprocess.run(["curl", "-sS", "-L", "--fail", "--max-time", "120", "-o", tmp, url],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 500:
        return (r.stderr or "download failed").strip()[:160]
    os.replace(tmp, path)
    return None

DROP_Q = re.compile(r"^(utm_.*|ref|src|source|gh_src|s|sid|trk|refid|lever-source.*|jobboardsource|iis|iisn|codes)$", re.I)

def urlkey(u):
    try:
        p = urlsplit(u.strip())
    except Exception:
        return u.strip().lower()
    host = p.netloc.lower().replace("www.", "")
    host = host.replace("boards.greenhouse.io", "job-boards.greenhouse.io")
    path = re.sub(r"/(apply|application)/?$", "", p.path.rstrip("/"))
    path = re.sub(r"^/[a-z]{2}-[A-Za-z]{2}/", "/", path)          # /en-US/ locale prefix
    q = [(k, v) for k, v in parse_qsl(p.query) if not DROP_Q.match(k)]
    return host + path.lower() + ("?" + urlencode(sorted(q)) if q else "")

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

def idkey(u, company):
    """Requisition id found in a posting URL, so the same job matches across feeds."""
    m = UUID.search(u)
    if m:
        return m.group(0).lower()
    p = urlsplit(u)
    runs = re.findall(r"\d{5,}", p.path + "?" + p.query)
    return (runs[-1].lstrip("0") + ":" + cokey(company)[:4]) if runs else None

BLOCK_URL, BLOCK_ID, OFFTERM = set(), set(), {}
AGGREGATOR = re.compile(r"builtin\.com|intern-list\.com|joinhandshake\.com|wellfound\.com|jobright\.ai|zapply\.jobs|simplify\.jobs|linkedin\.com|indeed\.com|glassdoor\.com", re.I)

def jid(u):
    return hashlib.sha1(urlkey(u).encode()).hexdigest()[:14]

CO_SUFFIX = re.compile(r"\b(inc|llc|l\.l\.c|corp|corporation|co|company|ltd|plc|lp|group|holdings|the)\b\.?")
def cokey(c):
    c = c.lower().replace("&", " and ")
    c = re.sub(r"\(.*?\)", " ", c)
    c = CO_SUFFIX.sub(" ", c)
    return re.sub(r"[^a-z0-9]+", "", c)

def tkey(t):
    t = t.lower()
    t = re.sub(r"\b(summer|2027|internships?|interns?|co-?op|usa?|united states|u\.s\.)\b", " ", t)
    return re.sub(r"[^a-z0-9]+", "", t)

STOP = set("summer 2027 intern internship interns internships co op coop us usa the and of for in a to at program".split())
def toks(t):
    out = set()
    for w in re.findall(r"[a-z0-9+#]+", t.lower()):
        w = re.sub(r"(ing|ment|s)$", "", w) if len(w) > 5 else w
        if w not in STOP and len(w) > 1:
            out.add(w)
    return out

def canon(company):
    k = cokey(company)
    for p in ("tiktok", "bytedance", "amazon", "google", "microsoft", "nvidia", "capitalone", "jpmorgan", "idemia", "hpe", "bae"):
        if k.startswith(p):
            return p
    return k

def fuzzy_dup(company, title, titles):
    """True when the same company already has a listing whose title words contain, or are contained in, this one."""
    mine = toks(title)
    if len(mine) < 2:
        return True
    for other in titles.get(canon(company), []):
        if len(other) >= 2 and (mine <= other or other <= mine):
            return True
    return False

def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", "", s or ""))
    s = re.sub(r"[\U0001F000-\U0001FAFF☀-➿↳️]", "", s)
    return re.sub(r"\s+", " ", s).strip(" -|*")

# ------------------------------------------------------------- locations
STATES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
STATE_NAMES = ("alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho illinois "
               "indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota mississippi missouri montana "
               "nebraska nevada hampshire jersey mexico york carolina dakota ohio oklahoma oregon pennsylvania rhode tennessee texas "
               "utah vermont virginia washington wisconsin wyoming").split()
NON_US = re.compile(r"\b(canada|ontario|quebec|british columbia|alberta|toronto|vancouver|montreal|waterloo|ottawa|calgary|"
                    r"united kingdom|uk|england|london|ireland|dublin|india|bangalore|bengaluru|hyderabad|pune|gurgaon|chennai|mumbai|"
                    r"germany|berlin|munich|france|paris|netherlands|amsterdam|israel|tel aviv|singapore|china|shanghai|beijing|shenzhen|"
                    r"japan|tokyo|australia|sydney|melbourne|switzerland|zurich|poland|warsaw|krakow|mexico city|guadalajara|brazil|"
                    r"sao paulo|spain|madrid|barcelona|sweden|stockholm|korea|seoul|taiwan|taipei|hong kong|romania|portugal|lisbon|"
                    r"costa rica|philippines|malaysia|vietnam|egypt|uae|dubai|emea|apac|latam|europe|on|bc|qc|ab)\b", re.I)
US_WORD = re.compile(r"\b(usa|u\.s\.a?\.?|united states|us)\b", re.I)
LA = re.compile(r"\b(los angeles|santa monica|culver city|el segundo|pasadena|burbank|glendale|long beach|irvine|torrance|hawthorne|"
                r"playa vista|venice|westwood|beverly hills|manhattan beach|redondo beach|costa mesa|newport beach|anaheim|thousand oaks|"
                r"woodland hills|calabasas|inglewood|gardena|carson|chatsworth|simi valley|van nuys|west hollywood|hollywood|orange county|"
                r"huntington beach|santa ana|aliso viejo|camarillo|westlake village|agoura hills|marina del rey|playa del rey)\b", re.I)

def is_us(loc):
    if US_WORD.search(loc):
        return True
    toks = re.findall(r"[A-Za-z]+", loc)
    has_state = any(t in STATES for t in toks) or any(t.lower() in STATE_NAMES for t in toks)
    if NON_US.search(loc) and not has_state:
        return False
    if re.search(r"\b(ON|BC|QC|AB)\b", loc) and re.search(r"canada|toronto|vancouver|montreal|waterloo|ottawa|calgary", loc, re.I):
        return False
    return True      # unknown strings count as US (lists are US-focused)

def tidy_loc(loc):
    loc = clean(loc)
    if re.match(r"remote", loc, re.I):
        return "Remote (US)"
    m = re.match(r"United States[- ]+([A-Za-z ]+?)-([A-Za-z .]+)$", loc)
    if m:
        return f"{m.group(2).strip()}, {m.group(1).strip()}"
    loc = re.sub(r",?\s*(United States of America|United States|USA|US)\s*$", "", loc)
    loc = re.sub(r"^(US|USA),\s*", "", loc)
    return loc.strip(" ,") or "United States"

def split_locs(v):
    if isinstance(v, list):
        out = v
    else:
        out = re.split(r"\s*;\s*|\s*\|\s*|\s*</br>\s*|\s*<br\s*/?>\s*", v or "")
    return [x for x in (clean(o) for o in out) if x]

# ---------------------------------------------------------- classification
ADV_ONLY = re.compile(r"\bph\.?\s?d\b|\bdoctoral\b|\bmba\b|\bpostdoc|\bjd\b", re.I)
MASTERS = re.compile(r"\bmaster'?s?\b|\bm\.?s\.?\b(?!\s*office)|\bgraduate\b|\bgrad\b", re.I)
UNDERGRAD = re.compile(r"undergrad|bachelor|\bb\.?s\.?\b|\bba\b|\bbs/ms\b", re.I)
OFFSEASON = re.compile(r"\b(fall|autumn|winter|spring|january|september|off[- ]?cycle|off[- ]?season)\b", re.I)
SUMMER = re.compile(r"\bsummer\b", re.I)
NOT_ROLE = re.compile(r"\b(market(ing)?|sales|account(ing|ant)?|financ(e|ial)(?! technology)|human resources|\bhr\b|legal|paralegal|recruit|"
                      r"talent|communications?|supply chain|logistics|procurement|mechanical|electrical(?! and computer)|civil|chemical|"
                      r"manufacturing|industrial|materials|aerospace(?! software)|structural|thermal|rf\b|analog|asic|vlsi|fpga|rtl|"
                      r"silicon|circuit|pcb|optical|photonic|process engineer|quality|test technician|technician|systems engineer(ing)?|"
                      r"product manage|program manage|project manage|product owner|product design|ux|ui/ux|graphic|industrial design|"
                      r"business (analyst|intelligence|development|operations)|operations (analyst|intern)|strategy|actuar|audit|tax|"
                      r"underwrit|investment|trading intern|trader|economist|policy|content|editor|writer|clinical|nurs|pharma|"
                      r"help ?desk|desktop support|it support|service desk|customer (success|support)|administrative|facilities|"
                      r"construction|environmental|health and safety|coordinator|operations|data cent(er|re)|laboratory|implementation|biostat|architect(ure)? intern|interior|real estate|biolog|chemist)\b", re.I)
ML_RE = re.compile(r"machine learning|\bml\b|\bai\b|\ba\.i\.|artificial intelligence|deep learning|\bllms?\b|\bnlp\b|natural language|"
                   r"computer vision|perception|generative|gen ?ai|agentic|\bagents?\b|reinforcement|applied scien|research engineer|"
                   r"mlops|autonomy|autonomous|data scien|foundation model|inference|recommendation|ranking", re.I)
DS_ONLY = re.compile(r"data scien", re.I)
DATA_ANALYST = re.compile(r"\banalyst\b|\banalytics\b|business intelligence|\bbi\b|data entry|data governance|data management|reporting", re.I)
SWE_RE = re.compile(r"software|developer|\bswe\b|\bsde\b|\bsdet\b|full[- ]?stack|front[- ]?end|back[- ]?end|\bweb\b|mobile|\bios\b|android|"
                    r"platform|infrastructure|devops|\bsre\b|site reliability|cloud|systems software|firmware|embedded|security engineer|"
                    r"cyber|appsec|application security|applications? engineer|programmer|compiler|game(play)? (engineer|programmer)|"
                    r"computer science|comp(uter)? sci|technology (intern|analyst|summer|program|development)|tech(nology)? intern|"
                    r"engineering intern|data engineer|database|distributed|backend|frontend|api\b|automation engineer|"
                    r"quantitative developer|quant dev|strategy developer|robotics software|simulation|graphics|kernel|"
                    r"network software|tooling|tools engineer|engineer(ing)? summer analyst|engineering \| summer analyst", re.I)

def _names(txt):
    return set(cokey(c) for c in txt.replace("\n", "").split("|") if c.strip())

TOP_A = _names("""Google|Alphabet|Meta|Meta Platforms|Apple|Amazon|Amazon Web Services|AWS|Microsoft|Netflix|NVIDIA|Tesla|SpaceX|Adobe|Salesforce|
Uber|Lyft|Airbnb|DoorDash|Instacart|Pinterest|Snap|Snap Inc|Reddit|LinkedIn|TikTok|ByteDance|Spotify|Dropbox|Atlassian|Intuit|Block|Square|
Shopify|Roblox|Riot Games|Anthropic|OpenAI|Google DeepMind|DeepMind|xAI|Scale AI|Cohere|Mistral AI|Perplexity|Hugging Face|Databricks|
Snowflake|Palantir|Palantir Technologies|Anduril|Anduril Industries|Waymo|Zoox|Figure|Figure AI|Cloudflare|Datadog|MongoDB|Stripe|Plaid|Ramp|
Brex|Robinhood|Coinbase|Affirm|Bloomberg|Jane Street|Citadel|Citadel Securities|Two Sigma|Hudson River Trading|Jump Trading|D. E. Shaw|
DE Shaw|The D. E. Shaw Group|Optiver|IMC|IMC Trading|Susquehanna|Susquehanna International Group|SIG|DRW|Akuna Capital|Five Rings|
Virtu Financial|Tower Research Capital|Old Mission|Point72|Millennium|Figma|Notion|Linear|Vercel|Supabase|Retool|Rippling|Airtable|Canva|
Discord|Duolingo|Asana|Verkada|Samsara|Glean|Harvey|Cursor|Anysphere|Cognition|Replit|Sierra|Decagon|ElevenLabs|Runway|Character.AI|
Together AI|Modal|Anyscale|Weights & Biases|LangChain|Pinecone|Hebbia|Abridge|Zip|Mercury|Nuro|Applied Intuition|Skydio|Shield AI|Zipline|
Neuralink|Wiz|GitHub|Slack|Twitch|Epic Games|Qualcomm|AMD|Capital One|Goldman Sachs|JPMorgan Chase|JPMorgan|Morgan Stanley""")
TOP_B = _names("""Intel|IBM|Oracle|Cisco|PayPal|Electronic Arts|Twilio|ServiceNow|Chime|Visa|Mastercard|American Express|BlackRock|
CrowdStrike|Palo Alto Networks|Okta|Zscaler|Snyk|Benchling|Flexport|Activision|Blizzard Entertainment|Sony Interactive Entertainment|
PlayStation|Nintendo|Unity|Unity Technologies|GitLab|HubSpot|Workday|Box|Splunk|Nutanix|Pure Storage|Rubrik|Cohesity|Confluent|Elastic|
HashiCorp|Chewy|Wayfair|Etsy|eBay|Zillow|Expedia|Expedia Group|Booking|Match Group|Tinder|Hinge|Quora|Yelp|Nextdoor|Grammarly|Miro|Webflow|
SoFi|Toast|Klaviyo|Gusto|Deel|Faire|Lucid Motors|Rivian|Cruise|General Motors|Boston Dynamics|Khan Academy|Disney|Walt Disney|Hulu|Garmin|
Arista Networks|Juniper Networks|The Trade Desk|AppLovin|Niantic|Roku|Sonos|Blue Origin|Rocket Lab|Relativity Space|Varda|Hadrian|Saronic|
Gecko Robotics|C3 AI|Kensho|Kensho Technologies|Citi|Bank of America|Wells Fargo|Fidelity|Fidelity Investments|Aurora|
Hewlett Packard Enterprise|HP|Dell|Dell Technologies|Samsung|Autodesk|VMware|Broadcom|Akamai|Zoom|DocuSign|Square|Lockheed Martin|
Northrop Grumman|Boeing|Tempus|Tempus AI|Motorola Solutions|T-Mobile|Verizon|Comcast|Warner Bros. Discovery|Paramount|NBCUniversal|
Nike|Walmart|Target|Home Depot|Ford|Toyota Research Institute|Honda|John Deere|Caterpillar|Siemens|Bosch|Micron|Texas Instruments""")
TOP_PREFIX = ("tiktok", "bytedance", "amazon", "google", "microsoft", "nvidia", "capitalone", "jpmorgan", "goldmansachs",
              "morganstanley", "apple", "metaplatforms", "salesforce", "adobe", "tesla", "spacex", "palantir", "anduril", "citadel")

def company_tier(company):
    k = cokey(company)
    if k in TOP_A or (k.startswith(TOP_PREFIX) and k not in ("applebees", "appleleisure")):
        return 2
    return 1 if k in TOP_B else 0

FIT = [   # (regex, points, tag) — matched to the resume
    (re.compile(r"\bllms?\b|gen ?ai|generative|agentic|\bagents?\b|\bnlp\b|natural language|\bai\b|artificial intelligence|foundation model", re.I), 6, "AI / LLM"),
    (re.compile(r"fine[- ]?tun|distill|inference|ml ?infra|mlops|ml platform|training|model (shaping|optim)|evaluation", re.I), 5, "ML systems"),
    (re.compile(r"security|cyber|appsec|trust (and|&) safety|privacy|integrity|red team|threat|safety", re.I), 6, "Security"),
    (re.compile(r"full[- ]?stack|front[- ]?end|back[- ]?end|backend|frontend|\bweb\b|react|typescript", re.I), 4, "Full-stack"),
    (re.compile(r"robot|autonom|gantry|lab automation|automation", re.I), 4, "Robotics"),
    (re.compile(r"developer (tools|experience|platform|productivity)|dev ?tools|tooling|platform|infrastructure|distributed|systems", re.I), 3, "Infra / tools"),
    (re.compile(r"python|c\+\+", re.I), 2, None),
]
UNFIT = [
    (re.compile(r"embedded|firmware|fpga|hardware|kernel driver|bios", re.I), -4),
    (re.compile(r"\.net|c#|mainframe|cobol|\bsap\b|salesforce (developer|admin)|servicenow developer|sharepoint|power ?bi|abap", re.I), -5),
    (re.compile(r"\bqa\b|quality assurance|test engineer|sdet|validation|verification", re.I), -4),
    (re.compile(r"network (engineer|operations)|\bit\b|information technology|sysadmin|systems administrat", re.I), -6),
    (re.compile(r"data engineer|etl|database admin", re.I), -2),
    (re.compile(r"clearance|ts/sci|secret", re.I), -4),
]

STRONG = re.compile(r"software|developer|\bswe\b|\bsde\b|full[- ]?stack|front[- ]?end|back[- ]?end|backend|frontend|programmer|devops|"
                    r"\bsre\b|site reliability|cyber|security engineer|computer science|data engineer|firmware|embedded|compiler|"
                    r"machine learning|\bml\b|\bai\b|deep learning|\bllms?\b|computer vision|data scien|applied scien|research engineer", re.I)

BIZ = re.compile(r"\b(market(ing)?|sales|account(ing|ant)?|human resources|\bhr\b|legal|recruit|talent|communications?|supply chain|"
                 r"procurement|product manage|program manage|project manage|product owner|business (analyst|development|operations)|"
                 r"strategy|coordinator|administrative|customer (success|support)|content|editor|writer|policy)\b", re.I)
BUILDER = re.compile(r"software|developer|engineer|programmer|scientist|\bswe\b|\bsde\b", re.I)

def classify(title, hint, loose=True):
    """Return 'swe' | 'ml' | 'ds' | None. hint is the feed's own category;
    loose lets a curated software list vouch for a vague engineering title."""
    t = title
    if ADV_ONLY.search(t) and not UNDERGRAD.search(t):
        return None
    if MASTERS.search(t) and not UNDERGRAD.search(t):
        return None
    if OFFSEASON.search(t) and not SUMMER.search(t):
        return None
    if re.search(r"\b2026\b", t) and not re.search(r"\b2027\b", t):
        return None
    if re.search(r"new grad|full[- ]time(?! student)|apprentice", t, re.I):
        return None
    ml, swe = bool(ML_RE.search(t)), bool(SWE_RE.search(t))
    if NOT_ROLE.search(t) and not STRONG.search(t):
        return None
    if BIZ.search(t) and not BUILDER.search(t):
        return None
    if hint == "ml":
        if ml and not (DATA_ANALYST.search(t) and not re.search(r"engineer|scien", t, re.I)):
            return "ds" if (DS_ONLY.search(t) and not re.search(r"machine learning|\bml\b|\bai\b|engineer", t, re.I)) else "ml"
        return "swe" if swe and not DATA_ANALYST.search(t) else None
    if ml and re.search(r"machine learning|\bml\b|\bai\b|deep learning|\bllm|computer vision|perception|research engineer|applied scien|autonomy|generative|gen ?ai|agentic|mlops", t, re.I):
        return "ml"
    if swe:
        return "swe"
    if loose and hint == "swe" and not NOT_ROLE.search(t) and re.search(r"engineer|develop|program|technolog|comput", t, re.I):
        return "swe"
    return None

def score(company, title, locs, kind):
    tags, pts = [], {"swe": 18, "ml": 18, "ds": 9}[kind]
    add = 0
    for rx, p, tag in FIT:
        if rx.search(title):
            add += p
            if tag and tag not in tags:
                tags.append(tag)
    pts += min(add, 12)
    pts += min(0, sum(p for rx, p in UNFIT if rx.search(title)))
    tier = company_tier(company)
    if tier == 2:
        pts += 20
        tags.insert(0, "Top company")
    elif tier == 1:
        pts += 10
    joined = " | ".join(locs)
    if LA.search(joined):
        pts += 8
        tags.append("LA area")
    elif re.search(r"\bCA\b|california", joined):
        pts += 4
        tags.append("California")
    elif re.search(r"remote", joined, re.I):
        pts += 4
        tags.append("Remote")
    if re.search(r"sophomore|second[- ]year|first[- ]year|freshm|underclass|explore program|\bstep\b|ignite|early (insight|talent|id)", title, re.I):
        tags.append("Underclass program")
    if re.search(r"clearance|ts/sci", title, re.I):
        tags.append("Clearance")
    if re.search(r"u\.?s\.? (person|citizen)|citizenship", title, re.I):
        tags.append("US persons only")
    return max(0, pts), tags[:4]

def grad_ok(text):
    """1 = fits a June 2028 graduate, 0 = does not, 2 = check, None = unknown."""
    if not text:
        return None
    t = text.lower()
    if re.search(r"completed (the|their|your) (second|sophomore)", t):
        return 1
    if re.search(r"second[- ]year|sophomore|first[- ]year|freshm", t):
        return 2
    if re.search(r"202[89]", t):
        return 1
    if re.search(r"or later|and beyond|no earlier than|or after|after the internship|return(ing)? to (school|coursework|a degree)|"
                 r"remaining|penultimate|final year|junior|rising senior|enrolled in a bachelor|after the completion|"
                 r"current undergraduate|undergraduate or|bachelor'?s,? (or )?master|pursuing a bachelor|towards a bachelor", t):
        return 1
    if re.search(r"202[67]", t):
        return 0
    return None

# ----------------------------------------------------------------- parsers
def rec(src, company, title, url, locs, posted, hint, est=0, extra_tags=None):
    return {"src": src, "c": clean(company), "t": clean(title), "u": url.strip(), "l": locs, "p": int(posted),
            "hint": hint, "pe": est, "xt": extra_tags or []}

def parse_simplify(path, src):
    out = []
    for x in json.load(open(path, encoding="utf-8")):
        deg = x.get("degrees") or []
        adv = bool(deg) and not any(d.startswith(("Bachelor", "Associate")) for d in deg)
        terms = x.get("terms") or []
        if not (x.get("active") and x.get("is_visible", True)) or adv:
            # Simplify says closed, hidden, or advanced-degree only: keep it off the board
            BLOCK_URL.add(urlkey(x.get("url", "")))
            k = idkey(x.get("url", ""), x.get("company_name", ""))
            if k:
                BLOCK_ID.add(k)
            continue
        if "Summer 2027" not in terms:
            # open, but Simplify files it under another term
            OFFTERM[urlkey(x.get("url", ""))] = terms
            k = idkey(x.get("url", ""), x.get("company_name", ""))
            if k:
                OFFTERM[k] = terms
            continue
        cat = (x.get("category") or "").lower()
        if cat.startswith("software"):
            hint = "swe"
        elif "ml" in cat or "machine" in cat or "data" in cat:
            hint = "ml"
        elif cat.startswith("quant"):
            hint = "quant"
        else:
            continue
        out.append(rec(src, x["company_name"], x["title"], x["url"], split_locs(x.get("locations")), x["date_posted"], hint))
    return out

def parse_vansh(path, src):
    out = []
    for x in json.load(open(path, encoding="utf-8")):
        if not (x.get("active") and x.get("is_visible", True)):
            continue
        if "summer" not in str(x.get("season", "")).lower() or x.get("date_posted", 0) < 1780272000:   # before 2026-06-01 = last cycle
            continue
        tags = ["US citizens only"] if "citizen" in str(x.get("sponsorship", "")).lower() else []
        out.append(rec(src, x["company_name"], x["title"], x["url"], split_locs(x.get("locations")), x["date_posted"], None, 0, tags))
    return out

def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def parse_zshah(path, src):
    out = []
    for x in json.load(open(path, encoding="utf-8")).get("jobs", []):
        season, title = x.get("season") or "", x.get("title") or ""
        if season != "Summer 2027" and not (season == "Not stated" and re.search(r"2027|summer", title, re.I)):
            continue
        cat = (x.get("category") or "").lower()
        hint = "swe" if cat in ("software", "security") else "ml" if "ml" in cat or "data" in cat else "quant" if cat == "quant" else None
        if hint is None or not x.get("url"):
            continue
        try:
            p = iso(x["posted_at"]) if x.get("posted_at") else iso(x["first_seen_at"])
        except Exception:
            continue
        est = 0
        if x.get("posted_at_source") != "exact":
            p, est = min(p + 12 * 3600, NOW - 3600), 1          # date only: assume midday
        tags = ["Security"] if cat == "security" else []
        out.append(rec(src, x["company"], title, x["url"], split_locs(x.get("location")), p, hint, est, tags))
    return out

def parse_speedy(path, src):
    out, on = [], False
    hint = "ml" if src == "sa" else "swe"
    for line in open(path, encoding="utf-8", errors="replace"):
        if "TABLE" in line and "START" in line:
            on = True
            continue
        if "TABLE" in line and "END" in line:
            on = False
            continue
        if not on or not line.startswith("| <a"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if len(cells) < 5:
            continue
        m = re.search(r'href="([^"]+)"', cells[-2])
        a = re.match(r"(\d+)\s*d", cells[-1])
        if not m or not a:
            continue
        posted = NOW - int(a.group(1)) * 86400 - 6 * 3600
        out.append(rec(src, cells[0], cells[1], m.group(1), split_locs(cells[2]), posted, hint, 1))
    return out

MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}

def parse_jobright(path, src):
    out, last = [], ""
    year = datetime.fromtimestamp(NOW, timezone.utc).year
    for line in open(path, encoding="utf-8", errors="replace"):
        if not line.startswith("| ") or "jobright.ai/jobs" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if len(cells) < 5:
            continue
        cm = re.search(r"\[([^\]]+)\]", cells[0])
        company = cm.group(1) if cm else last
        last = company
        tm = re.search(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", cells[1])
        dm = re.match(r"([A-Za-z]{3})\s+(\d{1,2})", cells[-1])
        if not tm or not dm or dm.group(1).lower() not in MONTHS or not company:
            continue
        p = datetime(year, MONTHS[dm.group(1).lower()], int(dm.group(2)), 19, tzinfo=timezone.utc).timestamp()
        if p > NOW + 86400:
            p = datetime(year - 1, MONTHS[dm.group(1).lower()], int(dm.group(2)), 19, tzinfo=timezone.utc).timestamp()
        # Jobright stamps rows with its own crawl date, so never treat them as posted today
        out.append(rec(src, company, tm.group(1), tm.group(2).split("?")[0], split_locs(cells[2]), min(p, NOW - 3 * 86400), "swe", 1))
    return out

def parse_zapply(path, src):
    out, hint = [], None
    unit = {"m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000}
    for line in open(path, encoding="utf-8", errors="replace"):
        if "<summary>" in line:
            s = clean(line).lower()
            hint = "swe" if "software" in s else "ml" if "data science" in s else None
            continue
        if hint is None or not line.startswith("| **"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if len(cells) < 6:
            continue
        m = re.search(r"\]\((https?://[^)\s]+)\)", cells[-1])
        a = re.match(r"(\d+)\s*(mo|m|h|d|w)\b", cells[3])
        if not m or not a:
            continue
        out.append(rec(src, cells[0], cells[1], m.group(1).split("?")[0], split_locs(cells[2]), NOW - int(a.group(1)) * unit[a.group(2)], hint, 1))
    return out

PARSERS = {"simplify": parse_simplify, "vansh": parse_vansh, "zshah": parse_zshah, "speedy": parse_speedy,
           "jobright": parse_jobright, "zapply": parse_zapply}

# -------------------------------------------------------------------- main
def finish(r):
    """Filter + score one raw record. Returns a board item or None."""
    locs = [l for l in r["l"] if is_us(l)]
    if r["l"] and not locs:
        return None
    hint = r["hint"]
    if hint == "quant":
        if not re.search(r"software|developer|engineer|\bswe\b", r["t"], re.I):
            return None
        hint = "swe"
    kind = classify(r["t"], hint, loose=r["src"] in ("si", "sp", "zs", "web"))
    if kind is None:
        return None
    locs = [tidy_loc(l) for l in locs]
    b, tags = score(r["c"], r["t"], locs, kind)
    for x in r["xt"]:
        if x not in tags:
            tags.append(x)
    item = {"i": jid(r["u"]), "c": r["c"][:60], "t": r["t"][:140], "u": r["u"], "l": locs[:4], "p": r["p"],
            "k": "ml" if kind in ("ml", "ds") else "swe", "s": r["src"], "b": b, "g": tags[:5]}
    if len(locs) > 4:
        item["m"] = len(locs) - 4
    if r["pe"]:
        item["e"] = 1
    return item

def load_db_doc(db, coll, doc):
    if not db:
        return {}
    p = os.path.join(db, coll, doc + ".json")
    if not os.path.exists(p):
        return {}
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    for key in ("data", "fields", "body"):            # tolerate a wrapped save format
        if isinstance(d.get(key), dict) and ("items" in d[key]):
            return d[key]
    return d

def fresh_pts(p):
    h = (NOW - p) / 3600
    return 38 if h < 24 else 28 if h < 48 else 22 if h < 72 else 15 if h < 168 else 9 if h < 336 else 4 if h < 720 else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--db")
    ap.add_argument("--cand")
    a = ap.parse_args()
    raw = os.path.join(a.out, "raw")
    os.makedirs(raw, exist_ok=True)

    items, seen_url, seen_id, seen_ct, titles, stats, errors = [], set(), set(), {}, {}, [], []
    ct_id, ik_id = {}, {}
    for code, label, url, kind in SOURCES:
        path = os.path.join(raw, code)
        err = fetch(url, path)
        st = {"code": code, "label": label, "found": 0, "kept": 0, "ok": True}
        if err and not os.path.exists(path):
            st["ok"] = False
            errors.append(f"{label}: {err}")
            stats.append(st)
            continue
        if err:
            errors.append(f"{label}: using earlier copy ({err})")
        try:
            recs = PARSERS[kind](path, code)
        except Exception as ex:                                    # a feed changed shape
            st["ok"] = False
            errors.append(f"{label}: could not read ({type(ex).__name__}: {ex})"[:200])
            stats.append(st)
            continue
        st["found"] = len(recs)
        if not recs:
            st["ok"] = False
            errors.append(f"{label}: no rows parsed")
        for r in recs:
            if not r["u"].startswith("http") or not r["c"] or not r["t"]:
                continue
            it = finish(r)
            if it is None:
                continue
            uk, ck, ik = urlkey(r["u"]), (cokey(r["c"]), tkey(r["t"])), idkey(r["u"], r["c"])
            if uk in seen_url or (ik and ik in seen_id):
                continue
            if code != "si":
                if uk in BLOCK_URL or (ik and ik in BLOCK_ID):
                    st["closed"] = st.get("closed", 0) + 1
                    continue
                off = OFFTERM.get(uk) or (ik and OFFTERM.get(ik))
                if off and not re.search(r"2027|summer", r["t"], re.I):
                    st["offterm"] = st.get("offterm", 0) + 1
                    continue
                if ck in seen_ct and seen_ct[ck] != code:
                    continue
                if code in ("jr", "za") and fuzzy_dup(r["c"], r["t"], titles):
                    continue
            seen_url.add(uk)
            if ik:
                seen_id.add(ik)
            seen_ct.setdefault(ck, code)
            ct_id.setdefault(ck, it["i"])
            if ik:
                ik_id.setdefault(ik, it["i"])
            titles.setdefault(canon(r["c"]), []).append(toks(r["t"]))
            items.append(it)
            st["kept"] += 1
        stats.append(st)

    by_id = {it["i"]: it for it in items}
    by_url = {urlkey(it["u"]): it for it in items}

    # ---- web finds + eligibility notes carried forward and merged
    old_extra = load_db_doc(a.db, "aux", "extra").get("items", [])
    notes = load_db_doc(a.db, "aux", "notes").get("items", {})
    if not isinstance(notes, dict):
        notes = {}
    cands = json.load(open(a.cand, encoding="utf-8")) if a.cand else []
    extra, closed, added = {}, set(), 0
    for c in cands:
        if c.get("closed") and c.get("url"):
            closed.add(jid(c["url"]))
    for x in old_extra:                                           # keep earlier finds for 30 days
        if x.get("i") in closed or NOW - x.get("a", 0) > 30 * 86400:
            continue
        extra[x["i"]] = x
    for c in cands:
        url = (c.get("url") or "").strip()
        if not url.startswith("http") or c.get("closed"):
            continue
        i = jid(url)
        ik = idkey(url, c.get("company") or "")
        ck = (cokey(c.get("company") or ""), tkey(c.get("title") or ""))
        known = i if i in by_id else (by_url.get(urlkey(url)) or {}).get("i") or ik_id.get(ik) or (ct_id.get(ck) if c.get("title") else None)
        g = clean(c.get("grad") or c.get("grad_requirement") or "")[:160]
        if g:
            notes[known or i] = {"g": g, "ok": grad_ok(g), "at": int(NOW)}
        if known or not c.get("company") or not c.get("title"):
            continue
        if i in extra:
            extra[i]["a"] = int(NOW)                             # re-confirmed
            continue
        if urlkey(url) in BLOCK_URL or (ik and ik in BLOCK_ID):
            continue
        if AGGREGATOR.search(url) and fuzzy_dup(c["company"], c["title"], titles):
            continue
        posted, est = NOW, 1
        if c.get("posted"):
            try:
                posted, est = min(iso(c["posted"] + "T19:00:00+00:00"), NOW - 3600), 1
            except Exception:
                pass
        elif not c.get("fresh"):
            posted = NOW - 10 * 86400                             # undated web find: don't rank it as new
        r = rec("web", c["company"], c["title"], url, split_locs(c.get("locations") or []), posted,
                "ml" if c.get("category") == "ml" else "swe", est)
        it = finish(r)
        if it is None:
            continue
        it["a"] = int(NOW)
        if c.get("note"):
            it["n"] = clean(c["note"])[:140]
        extra[i] = it
        added += 1
    for i in list(extra):                                         # a feed has it now
        x = extra[i]
        ik = idkey(x["u"], x["c"])
        if i in by_id or (ik and (ik in seen_id or ik in BLOCK_ID)) or (cokey(x["c"]), tkey(x["t"])) in seen_ct:
            del extra[i]
    extra_items = list(extra.values())
    live = set(by_id) | set(extra)
    notes = {k: v for k, v in notes.items() if k in live or NOW - v.get("at", 0) < 45 * 86400}

    # ---- write
    chunks = [[] for _ in range(N_CHUNKS)]
    for it in items:
        chunks[zlib.crc32(it["i"].encode()) % N_CHUNKS].append(it)
    for n, ch in enumerate(chunks):
        ch.sort(key=lambda x: -x["p"])
        body = json.dumps({"at": int(NOW), "items": ch}, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode()) > 240_000:
            errors.append(f"chunk c{n} is {len(body.encode())} bytes; raise N_CHUNKS")
        open(os.path.join(a.out, f"c{n}.json"), "w", encoding="utf-8").write(body)
    json.dump({"at": int(NOW), "items": extra_items}, open(os.path.join(a.out, "extra.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    json.dump({"at": int(NOW), "items": notes}, open(os.path.join(a.out, "notes.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))

    allitems = items + extra_items
    new24 = [x for x in allitems if NOW - x["p"] < 86400]
    meta = {"at": int(NOW), "total": len(allitems), "new24": len(new24), "swe": sum(x["k"] == "swe" for x in allitems),
            "ml": sum(x["k"] == "ml" for x in allitems), "web": len(extra_items), "sources": stats, "errors": errors[:8],
            "gradYear": GRAD}
    json.dump(meta, open(os.path.join(a.out, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))

    ranked = sorted(allitems, key=lambda x: -(x["b"] + fresh_pts(x["p"])))
    lines = [f"scan at {datetime.fromtimestamp(NOW, timezone.utc):%Y-%m-%d %H:%M} UTC",
             f"total {len(allitems)} (swe {meta['swe']}, ml {meta['ml']}, web finds {len(extra_items)}, added from candidates {added})",
             f"posted in last 24h: {len(new24)}", "sources: " + "; ".join(f"{s['label']} {s['kept']}/{s['found']}" + ("" if s["ok"] else " FAILED") for s in stats)]
    if errors:
        lines.append("errors: " + " | ".join(errors))
    lines.append("")
    lines.append("TOP NEW (last 72h) — score | company | title | location | url")
    for x in [y for y in ranked if NOW - y["p"] < 3 * 86400][:25]:
        lines.append(f"{x['b'] + fresh_pts(x['p'])} | {x['c']} | {x['t']} | {', '.join(x['l'][:2])} | {x['u']}")
    lines.append("")
    lines.append("ELIGIBILITY NOT CHECKED YET (highest priority first) — id | company | title | url")
    todo = [x for x in ranked if x["i"] not in notes][:15]
    for x in todo:
        lines.append(f"{x['i']} | {x['c']} | {x['t']} | {x['u']}")
    open(os.path.join(a.out, "report.txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines[:5 + (1 if errors else 0)]))
    sizes = [os.path.getsize(os.path.join(a.out, f"c{n}.json")) for n in range(N_CHUNKS)]
    print("chunk bytes:", sizes)
    if sum(s["ok"] for s in stats) == 0 or len(items) < 100:
        print("SCAN FAILED: too few listings; do not overwrite the board.")
        sys.exit(2)

if __name__ == "__main__":
    main()

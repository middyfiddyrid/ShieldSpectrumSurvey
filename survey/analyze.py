"""Multi-engine threat-vector analysis.

ANALYZE fans the case packet out to every AI provider with a configured key
(OpenAI/ChatGPT, xAI/Grok, DeepSeek, Google/Gemini, Anthropic/Claude), lets
each choose its own presentation, then renders everything into a Shield-
branded CONFIDENTIAL INTERNAL MEMORANDUM (HTML -> PDF).

The packet sent to EXTERNAL providers is DE-IDENTIFIED (Person A/B/C, roles
and facts only — no names, addresses, or contact data). Real identities are
re-attached only in the locally rendered memo, which never leaves disk.
"""

import concurrent.futures
import html
import json
import os
import re
import time

import requests
from jinja2 import Template

from .report_html import TEMPLATE_DIR, _b64, _print_pdf

TIMEOUT = 90

PROVIDERS = {
    "ChatGPT (OpenAI)": {"key": "openai", "kind": "openai",
                         "url": "https://api.openai.com/v1/chat/completions",
                         "model": "gpt-4o"},
    "Grok (xAI)": {"key": "xai", "kind": "openai",
                   "url": "https://api.x.ai/v1/chat/completions",
                   "model": "grok-3"},
    "DeepSeek": {"key": "deepseek", "kind": "openai",
                 "url": "https://api.deepseek.com/chat/completions",
                 "model": "deepseek-chat"},
    "Gemini (Google)": {"key": "gemini", "kind": "gemini",
                        "url": "https://generativelanguage.googleapis.com/v1beta/"
                               "models/gemini-2.0-flash:generateContent"},
    "Claude Fable (Anthropic)": {"key": "anthropic", "kind": "anthropic",
                                 "url": "https://api.anthropic.com/v1/messages",
                                 "model": "claude-fable-5"},
}


def build_packet(db, session_id: int) -> dict:
    """De-identified case packet — safe to send to external providers."""
    eng = db.query_one("SELECT * FROM engagement WHERE session_id=?", (session_id,)) or {}
    pois = db.query("SELECT * FROM persons_of_interest WHERE session_id=? ORDER BY id",
                    (session_id,))
    letters = "ABCDEFGH"
    legend, subjects = [], []
    for i, p in enumerate(pois):
        tag = f"Person {letters[i]}"
        legend.append({"tag": tag, "name": p["name"]})
        subjects.append({
            "tag": tag,
            "relationship_to_clients": p.get("relationship") or "",
            "client_named": bool(p.get("named_by_client")),
            "motive_hypothesis": p.get("motive") or "",
            "physical_access": f"{p.get('access_tier') or 'unknown'} — "
                               f"{p.get('access_note') or ''}",
            "technical_capability": f"{p.get('capability_tier') or 'unknown'}"
                                    + (f" — {p['capability_basis']}"
                                       if p.get("capability_basis") else ""),
            "resources": p.get("resources_note") or "",
            "documented_conduct": p.get("conduct_note") or "",
            "opportunity_vectors": p.get("opportunity_vectors") or "",
            "analyst_position": p.get("analyst_notes") or "",
            "deconfliction_status": p.get("status") or "open",
        })
    packet = {
        "matter": "Residential TSCM / counterintelligence engagement for a married "
                  "couple (the Clients) following a smear campaign by their former "
                  "landlords. Names, addresses, and contact data are withheld; "
                  "subjects are referred to by letter.",
        "clients_situation": eng.get("client_concerns") or "",
        "subjects": subjects,
    }
    # scrub names that may appear inside free-text fields
    blob = json.dumps(packet)
    for item in legend:
        for word in item["name"].split():
            if len(word) > 3:
                blob = re.sub(re.escape(word), item["tag"].replace("Person ", "Subj"),
                              blob, flags=re.IGNORECASE)
    return {"packet": json.loads(blob), "legend": legend}


PROMPT = """You are contributing one section to a CONFIDENTIAL internal threat-vector
memorandum for a licensed technical surveillance counter-measures (TSCM) firm. The
engagement is defensive: the clients fear being monitored after a dormant dispute; the
firm's stated goal is to formally DISQUALIFY the named subjects as threat vectors and
provide de-confliction guidance, not to build a case against anyone.

THE CASE PACKET (de-identified):
{packet}

YOUR TASK — analyze the internal threat vectors and present your assessment however you
judge most useful: a narrative report, a threat matrix, a relationship/social graph
described in text or simple markup, a timeline — your choice. Address at least:
1. Ranked threat vectors (probability x impact), including the do-nothing baseline.
2. Relational/covert-aggression dynamics. Where relevant, apply the frameworks of
   Sam Vaknin (narcissistic injury, preemptive offense-as-defense, smear campaigns as
   protective aggression) and the general published literature on smear-campaign
   dynamics (decay without fuel, re-engagement amplification / Streisand effect).
3. Whether the firm's interim de-confliction position (do not re-engage; light
   de-escalation on incidental contact; document everything; complete the technical
   sweep and hardening) is sound, and any changes you would make.
4. Concrete disqualification criteria — what observed facts would let each subject be
   moved to CLEARED, and what would instead escalate them to CONCERN.

Constraints: hypothesis-framed (no assertions of fact about real persons), no legal
conclusions, no demographic inferences about capability. Plain text or simple markdown.
Begin with a one-line title for your section."""


def build_prompt(packet: dict) -> str:
    return PROMPT.format(packet=json.dumps(packet, indent=2))


def _call_openai_style(url, key, model, prompt):
    r = requests.post(url, timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "max_tokens": 3000,
                            "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _call_gemini(url, key, prompt):
    r = requests.post(f"{url}?key={key}", timeout=TIMEOUT,
                      json={"contents": [{"parts": [{"text": prompt}]}]})
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _call_anthropic(url, key, model, prompt):
    r = requests.post(url, timeout=TIMEOUT,
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                      json={"model": model, "max_tokens": 3000,
                            "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"])


def _call(name, spec, key, prompt):
    try:
        if spec["kind"] == "openai":
            return name, _call_openai_style(spec["url"], key, spec["model"], prompt), ""
        if spec["kind"] == "gemini":
            return name, _call_gemini(spec["url"], key, prompt), ""
        return name, _call_anthropic(spec["url"], key, spec["model"], prompt), ""
    except Exception as e:  # noqa: BLE001 - report per-provider failure, don't die
        return name, "", f"{type(e).__name__}: {e}"


def _md_to_html(text: str) -> str:
    """Tiny markdown renderer: headings, bold, bullets, paragraphs, tables pass as <pre>."""
    out, bullets = [], []

    def flush():
        if bullets:
            out.append("<ul class='terms'>" + "".join(f"<li>{b}</li>" for b in bullets)
                       + "</ul>")
            bullets.clear()

    def inline(s):
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        return re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)

    for block in re.split(r"\n{2,}", text.strip()):
        lines = block.strip().split("\n")
        if all(ln.lstrip().startswith(("|",)) for ln in lines) and len(lines) > 1:
            flush()
            out.append("<pre class='mono'>" + html.escape(block) + "</pre>")
            continue
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            m = re.match(r"^(#{1,4})\s+(.*)", s)
            if m:
                flush()
                out.append(f"<h3>{inline(m.group(2))}</h3>")
            elif re.match(r"^[-*•]\s+", s):
                bullets.append(inline(re.sub(r"^[-*•]\s+", "", s)))
            elif re.match(r"^\d+\.\s+", s):
                bullets.append(inline(re.sub(r"^\d+\.\s+", "", s)))
            else:
                flush()
                out.append(f"<p>{inline(s)}</p>")
        flush()
    return "\n".join(out)


def run_analysis(db, cfg, session_id: int, reports_dir: str,
                 local_contributions: dict | None = None) -> dict:
    built = build_packet(db, session_id)
    prompt = build_prompt(built["packet"])

    jobs = []
    for name, spec in PROVIDERS.items():
        key = cfg.get("api_keys", spec["key"], default="")
        if key:
            jobs.append((name, spec, key))
    results, errors = {}, {}
    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            for name, text, err in ex.map(lambda j: _call(j[0], j[1], j[2], prompt), jobs):
                if text:
                    results[name] = text
                else:
                    errors[name] = err
    for name, text in (local_contributions or {}).items():
        results.setdefault(name, text)
    not_configured = [n for n, s in PROVIDERS.items()
                      if not cfg.get("api_keys", s["key"], default="")
                      and n not in results]
    if not results:
        raise RuntimeError("no AI providers configured (set api_keys in config.yaml) "
                           "and no local contribution supplied")

    eng = db.query_one("SELECT * FROM engagement WHERE session_id=?", (session_id,)) or {}
    session = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    company = cfg.get("branding", "company", default="Shield Security Systems")
    ctx = {
        "company": company,
        "logo_b64": _b64(cfg.get("branding", "logo_path", default="")),
        "report_id": f"SHD-{time.strftime('%Y')}-TSCM-{session_id:03d}-THREAT",
        "date_long": time.strftime("%B %d, %Y  %H:%M"),
        "client_name": html.escape(eng.get("client_name") or "—"),
        "address": html.escape(session["address"] if session else "—"),
        "legend": [{"tag": item["tag"], "name": html.escape(item["name"])}
                   for item in built["legend"]],
        "sections": [{"engine": html.escape(name), "body": _md_to_html(text)}
                     for name, text in results.items()],
        "errors": [{"engine": html.escape(n), "err": html.escape(e)}
                   for n, e in errors.items()],
        "not_configured": [html.escape(n) for n in not_configured],
        "year": time.strftime("%Y"),
    }
    with open(os.path.join(TEMPLATE_DIR, "internal_analysis.html"), encoding="utf-8") as f:
        rendered = Template(f.read()).render(**ctx)

    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(reports_dir, exist_ok=True)
    html_path = os.path.join(reports_dir, f"session{session_id}_threat_analysis_{ts}.html")
    pdf_path = os.path.join(reports_dir, f"session{session_id}_threat_analysis_{ts}.pdf")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(rendered)
    _print_pdf(html_path, pdf_path)
    db.execute("INSERT INTO reports (session_id,path,kind,generated_utc) VALUES (?,?,?,?)",
               (session_id, pdf_path, "THREAT_ANALYSIS", time.time()))
    return {"path": pdf_path, "kind": "THREAT_ANALYSIS",
            "engines": list(results.keys()),
            "errors": errors, "not_configured": not_configured}

# DCS — DeConfliction Systems (platform productization of the TSCM app)

The local single-user Shield TSCM spectrum-survey app is being turned into a
hosted, multi-tenant SaaS: **DCS (DeConfliction Systems / TSCM)**. Shield
Security Systems is tenant #1; the platform is white-label so Tony Thurman's
organization can later run under its own brand.

## Backend — Supabase (provisioned 2026-06-11)

- Project name: **DCS**  ·  ref/id: `noagvzbtfvfsmsbedznj`
- Org: `qwmgpwuakrvjxmfacquq` (middyfiddyrid's Org) · Region: us-east-2 (Ohio)
- Cost: **$0/mo** (free tier; 2 projects/org — the other is PSDiary)
- API URL: `https://noagvzbtfvfsmsbedznj.supabase.co`
- Publishable (client) key: `sb_publishable_Er1Ny8Mgf5gbVTTuGNMy1g_JwEdAULs`
  (safe in front-end code; the secret service_role key is NOT stored here)

## Why Supabase (vs raw Azure/AWS Blob)

Flat, predictable pricing (no metered-egress runaway like the PICPHIX Blob
bill); managed Postgres + auth + storage + **row-level security** for per-org
isolation enforced at the database layer; far less to build than rolling auth
by hand. Compute host for the Flask app (PDF rendering, AI calls) to follow —
also flat-rate (Render / Fly / Azure App Service), with hard budget alerts.

## Schema (migrations applied)

`dcs_tenancy_core` + `dcs_harden_security_definer`:

- **organizations** — tenants; white-label fields (display_name, logo_url,
  primary_color, accent_color, report_prefix e.g. SHD/DCS, seat_limit, plan).
  Seeded: Shield Security Systems (slug `shield`, prefix SHD, 10 seats).
- **memberships** — the "seat": user ↔ org ↔ role (owner | admin | operator).
  Seat-limit enforced by trigger (a license = a seat).
- **invitations** — an admin provisions a technician by email before signup.
- **assessments** — per-org domain data (cloud side of an engagement).
- RLS: per-org isolation via `private.is_member()` / `private.is_org_admin()`
  (in a non-exposed schema — not callable via REST). `create_organization()`
  RPC = create tenant + make caller owner; authenticated-only.

## Architecture reality

The **physical RF sweep needs the RF Explorer on a COM port** → must run on a
machine at the premises. So DCS is HYBRID: preliminary assessments (address →
baseline → POI → branded pre-sweep PDF + threat analysis) run 100% in the
cloud, multi-user; the on-site sweep stays a local companion that syncs up.

## Next steps

1. Public brand / domain decision: run DCS under **NMP Labs** (inherit NMP's
   existing domain authority) — landing/coming-soon page.
2. Compute host for the Flask app + wire it to Supabase (auth, then migrate
   SQLite domain tables → Postgres with org_id).
3. Auth UI: sign-up/login, org admin console (invite technicians, manage seats,
   white-label settings).
4. **Before Tony's real client data touches it: licensing / data-controller
   agreement (counsel).** Second org logging in = processing another company's
   PII under signed terms.

## Status update — 2026-06-11 (end of long session)

**Live now:**
- NMP Labs blog post on nationsmediapartners.com (Wix), with a "More from
  NMP Labs" PsDiary link (pagesixdiary.netlify.app).
- **"NMP Labs" nav tab** added to the live Wix site (via the Editor, driven
  through the Claude Chrome extension) — redirects to the DCS post. Published.
- NMP's own data-center landing page is already live at nmpdatacenter.netlify.app
  (clean, generic, no client names). Just needs a pro domain (see below).

**Demo deploy — prepped, NOT yet hosted (commit d6863e8):**
- Dockerfile + .dockerignore + render.yaml + requirements-demo.txt; app.py binds
  0.0.0.0:$PORT when hosted. Smoke-tested locally. Reports use reportlab fallback
  (no browser in the image).
- **Blocked on Matt:** (1) push this repo to a PRIVATE GitHub repo (gh CLI not
  installed; Matt HAS a GitHub account — he creates an empty private repo, we add
  remote + push using stored creds), (2) create a Render account, then Blueprint-
  deploy render.yaml, (3) wire a "Launch live demo" button into the NMP Labs post
  + a nav tab.

**OPEN BUILD — seed the demo with the ANONYMIZED signal wiki (Matt's ask):**
- The demo runs on empty demo.db, so the Signal Wiki tab is blank. It should show
  our anonymized library. SAFETY: raw `captures` SVG fingerprints carry the source
  client's address/session in their footer (the Kenwood leak we already caught).
  Seed must STRIP provenance first (reuse the sanitization in
  report_html._showcase_captures). The `case_studies` composites are already
  de-identified and safe to seed as-is. Build a seed script that populates demo.db
  with sanitized captures + case studies.

**Other open (need Matt's accounts/clicks):**
- datacenter.nationsmediapartners.com subdomain: Matt adds the custom domain in
  Netlify, pastes the DNS target; I add the CNAME via Wix Domain DNS API (only that
  record, never email/root); then point the Data Center tab at it.
- Cinematic hero + LICENSED (not copyrighted) optional play-button audio on the
  data-center page. (Matt wanted movie-score audio; declined — copyright/exposure.)

**Standing guardrails reaffirmed this session:** do NOT publish anything about
client McCullough (the local site-plan files are titled "McCullough Property" —
never deploy them); no client PII in any public/demo surface; no copyrighted audio.

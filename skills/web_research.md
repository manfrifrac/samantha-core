# Global Skills — Web Research, Scraping & Public Portals (`web_research`)

> **Domain**: Deep web data collection, anti-bot mitigation, public administration REST endpoint discovery, direct HTTP POST form automation, and source verification.  
> **Source Reference**: Production research and data intelligence pipelines across the Samantha Ecosystem.

---

## 1. Anti-Bot Bypass via Page 1 Multi-Query Targeting

- **Problem Solved**:
  Datacenter server IP ranges (e.g. VPS providers) encountering immediate HTTP 403 blocks or Cloudflare challenge pages when navigating deep pagination (`&start=20`, `&page=3`) on employment or tender aggregators.
- **Technical Explanation**:
  Web Application Firewalls (WAFs) monitor sequential pagination signatures. Scouting only the first page with fresh query parameters (`filter=recent`, `fromage=7` days) avoids behavioral rate limits.
- **Implementation Guide**:
  1. **Avoid Deep Pagination**: Replace deep pagination loops with **broad multi-query sweeps across page 1** (e.g., 20–30 specific keyword and domain combinations).
  2. **Canonical ID Extraction**: Parse the canonical entity key (e.g. `item_id=1f281d...`) from the page 1 response payload.
  3. **Direct Entity Verification**: Verify individual target URLs independently: `https://portal.example/view?id=<id>` ensuring HTTP Status 200 before persisting.

---

## 2. Direct REST Querying for Public Administration & Tender Portals

- **Problem Solved**:
  Rendering modern public administration SPA portals (Vue/Angular/React) via automated headless browsers consumes excessive RAM and frequently hits rendering timeouts.
- **Technical Explanation**:
  Most modern public portals fetch dynamic data from unauthenticated public REST endpoints. Querying these endpoints directly via Python `requests` / `httpx` is sub-second, robust, and consumes minimal server memory.
- **Implementation Guide**:
  1. Inspect network tab to identify backend REST search APIs.
  2. Query directly via standard HTTP client:
     ```python
     import requests

     headers = {
         "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
         "Content-Type": "application/json"
     }
     payload = {
         "text": "infrastructure",
         "status": ["OPEN"]
     }
     response = requests.post(
         "https://portal.example.gov/api/public/search?page=0&size=50",
         headers=headers,
         json=payload,
         timeout=10
     )
     data = response.json()
     ```
  3. Validate exact criteria (e.g., geography codes, internal exclusions) programmatically in memory.

---

## 3. Browserless Form Submissions via Direct HTTP POST

- **Problem Solved**:
  Submitting structured contact forms, CVs, or RFP registrations through heavy GUI browsers is slow and prone to UI timeout exceptions.
- **Technical Explanation**:
  Web form plugins (WordPress Forminator, Gravity Forms, standard contact endpoints) process inputs via standard `multipart/form-data` or `application/x-www-form-urlencoded` payloads dispatched to AJAX endpoints.
- **Implementation Guide**:
  1. Determine the destination endpoint (e.g. `/wp-admin/admin-ajax.php`) and required form action IDs.
  2. Build the multipart payload containing field dictionaries and binary file attachments:
     ```python
     files = {"cv_upload": ("resume.pdf", open("/tmp/docs/resume.pdf", "rb"), "application/pdf")}
     data = {"action": "submit_form_custom", "form_id": "123", "full_name": "Jane Doe"}
     res = requests.post("https://example.org/wp-admin/admin-ajax.php", data=data, files=files)
     ```
  3. Verify JSON responses (e.g., `{"success": true}`) to confirm backend delivery.

---

## 4. Diagnosing HTTP 403 Forbidden: User-Agent Policies vs IP Subnet Blocks

- **Problem Solved**:
  Receiving an HTTP 403 Forbidden error leading developers to conclude that the server IP is permanently blacklisted.
- **Technical Explanation**:
  Many public APIs and web archives do not block IP ranges, but strictly reject default library User-Agents (`python-requests`, `curl`, `urllib`) to prevent naive automated scraping.
- **Implementation Guide**:
  1. Perform an isolated diagnosis test using a standard modern browser User-Agent:
     ```bash
     curl -sI -A "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0" "https://target-url.org"
     ```
  2. If the response returns HTTP 200, configure explicit, authentic browser User-Agent headers across all scraper modules.

---

## 🎯 Model Routing Recommendations

- **Primary Engine**: **Gemini 2.5 Flash (`agy` CLI)**
  - Fast, high-throughput data extraction, keyword filtering, and structured JSON parsing.
- **Secondary Engine**: **Claude 3.7 Sonnet (`claude` CLI)**
  - Advanced qualification scoring, semantic matching of tender specifications, and deep document synthesis.

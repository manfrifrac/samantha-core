# Global Skills — Browser & CDP Automation (`browser_automation`)

> **Domain**: Headless and headful Chromium control, Chrome DevTools Protocol (CDP), X11/Xvfb OS-level input synthesis, complex enterprise iframe handling, and anti-bot verification workflows.  
> **Source Reference**: Production implementations across specialized research, scrapers, and operational coordinators.

---

## 1. Hardware-Level OS Trusted Click (`xdotool` on `DISPLAY=:99`) for Dynamic Enterprise Portals

- **Problem Solved**:
  Enterprise web applications (e.g. PeopleSoft Fluid UI, Oracle Cloud, banking portals, dynamic ATS platforms) block or instantly dismiss popup modals, autocomplete dropdowns, or lookup prompts when clicked using:
  1. `element.click()` in JavaScript (`Runtime.evaluate`): generates synthetic events (`isTrusted=false`) with zero coordinates `(0, 0)` and skips the standard `mousedown`/`mouseup` sequence.
  2. CDP `Input.dispatchMouseEvent`: while marked `isTrusted=true`, it fails to trigger OS-level window focus shifts (X11 Aura window). Input field blur events trigger asynchronous AJAX callbacks that tear down dropdown DOM fragments before user selection registers.
- **Technical Explanation**:
  Injecting hardware-level mouse clicks via `xdotool` on the virtual X11 server (`Xvfb :99`) utilizes the kernel-level `XTEST` extension. This synthesizes true hardware events upstream of Chromium, indistinguishable from a human operator interacting via VNC/noVNC.
- **Coordinate Translation Formula (DOM Viewport → X11 Screen)**:
  1. Calculate Viewport Coordinates:
     $$\text{clientX} = \text{iframeOffsetX} + r.\text{left} + (r.\text{width} / 2)$$
     $$\text{clientY} = \text{iframeOffsetY} + r.\text{top} + (r.\text{height} / 2)$$
  2. Apply Window Offset & Chrome Top Toolbar (143px):
     $$\text{ScreenX} = \text{round}(\text{screenLeft} + (\text{clientX} \times \text{dpr}))$$
     $$\text{ScreenY} = \text{round}(\text{screenTop} + 143 + (\text{clientY} \times \text{dpr}))$$
- **Implementation Guide**:
  - **Via MCP Tool**:
    ```python
    chrome_contabo_trusted_click(
        selector="a[id$='$prompt']", # CSS selector, XPath, or exact text
        tab_id=target_tab_id,
        display=":99"
    )
    ```
  - **Via Standalone Utility**:
    ```bash
    python3 core/trusted_click.py --tab-id <TAB_ID> --selector "a.prompt-icon"
    ```

---

## 2. Smart Prompt Fields & Hidden Code Binding in Complex Forms

- **Problem Solved**:
  In enterprise forms (PeopleSoft, SAP SuccessFactors), typing plain text strings into autocomplete lookup fields appears visually valid via client-side CSS transforms, but fails server-side validation upon submission (`aria-invalid="true"`).
- **Technical Explanation**:
  Smart prompt widgets store an internal alphanumeric identifier (e.g., Education Level: `G` for Bachelor, `I` for Master; Discipline: `0008` for Biology) in hidden shadow inputs, populated only when selected through the native modal lookup dialog.
- **Implementation Guide**:
  1. Trigger the lookup magnifying glass icon using **Trusted Click**.
  2. Allow the search modal DOM tree to stabilize (0.8s sleep).
  3. Locate and click the target row (or inject the validated backend code).
  4. Capture a verification screenshot using `chrome_contabo_screenshot` prior to advancing to the next wizard stage.

---

## 3. Resolving Nested iFrame Desynchronization Glitches

- **Problem Solved**:
  During multi-step wizards, the main iframe can freeze or render empty white space without emitting browser console errors.
- **Technical Explanation**:
  This stems from state desynchronization in nested iframe DOM trees within a single browser tab, not a browser crash. Server-side session cookies and step progression remain intact.
- **Implementation Guide**:
  1. Do not clear session cookies or restart the authentication flow.
  2. Open a **new browser tab sharing the existing session cookies** directly at the wizard URL.
  3. The server immediately re-attaches to the active session and renders the saved wizard step.
  4. Gracefully close the stalled tab.

---

## 4. Cloudflare Turnstile Bypass Routing via Residential Proxy / Local Surface

- **Problem Solved**:
  On form submissions, Cloudflare Turnstile triggers infinite challenge verification loops due to datacenter IP ASN reputation.
- **Technical Explanation**:
  Turnstile calculates automated bot risk scores based on incoming ASN reputation. Residential IPs connected via SSH tunnels pass verification without aggressive challenges.
- **Implementation Guide**:
  1. Perform all data entry, document staging, and form filling on the primary server browser (`:9224`).
  2. If a persistent Turnstile loop occurs at the final submission step, route the final submit click through a mirrored tab on the residential proxy/bridge (`:9222`).
  3. Confirm submission receipt and terminate the secondary tab.

---

## 5. Atomic Tab Garbage Collection & Memory Leaks

- **Problem Solved**:
  Automated research tasks spawning dozens of tabs over time, causing Chromium memory consumption to exceed host limits and degrading system responsiveness.
- **Technical Explanation**:
  Each headless tab allocates its own V8 isolate and GPU buffer. Unclosed tabs retain full DOM trees and script execution contexts.
- **Implementation Guide**:
  1. Always wrap tab lifecycles in `try / finally` blocks ensuring `Target.closeTarget` is called.
  2. Implement an automated tab reaper in monitoring daemons: tabs inactive for > 30 minutes with non-critical titles are automatically terminated.
  3. Always verify available tabs before launching high-concurrency scraping jobs:
     ```python
     tabs = chrome_list_tabs()
     if len(tabs) > 15:
         prune_stale_tabs(tabs)
     ```

---

## 6. Base64 Document Upload via CDP `DOM.setFileInputFiles`

- **Problem Solved**:
  Native file upload dialogs (`<input type="file">`) trigger OS-native file picker windows that block headless automation threads.
- **Technical Explanation**:
  CDP provides direct programmatic file injection into DOM file input elements via `DOM.setFileInputFiles`, completely bypassing the OS modal dialog.
- **Implementation Guide**:
  1. Stage the file on the local filesystem (e.g., `/tmp/docs/resume.pdf`).
  2. Resolve the target input node ID:
     ```python
     node = cdp.DOM.querySelector(nodeId=root_id, selector="input[type='file']")
     ```
  3. Inject the file path directly:
     ```python
     cdp.DOM.setFileInputFiles(nodeId=node['nodeId'], files=["/tmp/docs/resume.pdf"])
     ```
  4. Trigger change events if required:
     ```javascript
     document.querySelector("input[type='file']").dispatchEvent(new Event('change', { bubbles: true }));
     ```

---

## 🎯 Model Routing Recommendations

- **Primary Engine**: **Gemini 2.5 Pro / Flash (`agy` CLI) or Claude 3.7 Sonnet**
  - High precision in multi-step CDP tool invocation and multimodal screenshot verification.
- **Secondary Engine**: **Claude Opus**
  - Reserved for highly complex multi-domain portal orchestration requiring deep structural analysis of obfuscated DOM trees.

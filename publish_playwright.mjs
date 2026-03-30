#!/usr/bin/env node
/**
 * Multi-platform publish via Playwright chromium.launchPersistentContext (CDP-backed).
 * No Selenium / no ChromeDriver — same “persistent profile = 免重复登录” model as TipKay-style stacks.
 *
 * Usage: node publish_playwright.mjs <payload.json>
 * Payload: { platforms: [...], article: {title, body, body_html?, body_plain?}, coverPath: string|null, wait: {page, upload}, headless?: boolean }
 */

import { execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";

/** 追加到 publish.log（与 main.py log_line 时间格式一致，便于全流程排查） */
function publishStepLog(publishLogPath, message) {
  if (!publishLogPath || typeof publishLogPath !== "string") return;
  try {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    const ts = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    fs.appendFileSync(publishLogPath, `[${ts}] step [playwright] ${message}\n`, "utf8");
  } catch {
    /* ignore disk errors */
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function splitSelectors(selectorString) {
  const s = String(selectorString || "").trim();
  if (!s) {
    return [];
  }
  if (s.includes("|")) {
    return s
      .split("|")
      .map((x) => x.trim())
      .filter(Boolean);
  }
  // 兼容 YAML 里用英文逗号拼接的多 selector（等价 CSS 列表）
  if (s.includes(",")) {
    return s
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
  }
  return [s];
}

function inspectSingletonLock(userDataDir) {
  const lockPath = path.join(userDataDir, "SingletonLock");
  if (!fs.existsSync(lockPath)) {
    return null;
  }
  let matching = [];
  try {
    const out = execSync("ps ax -o pid=,command=", {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    });
    const lines = out
      .split("\n")
      .map((x) => x.trim())
      .filter(Boolean);
    const marker = `--user-data-dir=${userDataDir}`;
    matching = lines.filter((line) => line.includes(marker));
  } catch {
    // Ignore ps failure; still surface lock presence.
  }
  return {
    lockPath,
    matching,
  };
}

async function saveFailureScreenshot(page, screenshotDir, platName, kind) {
  try {
    if (!page || page.isClosed?.()) {
      return "";
    }
    fs.mkdirSync(screenshotDir, { recursive: true });
    const ts = new Date().toISOString().replace(/[:.]/g, "-");
    const shot = path.join(screenshotDir, `${platName || "unknown"}-${kind}-${ts}.png`);
    await page.screenshot({ path: shot, fullPage: true });
    return shot;
  } catch {
    return "";
  }
}

async function hasAnyVisible(page, selectorString, timeoutMs = 8000) {
  const parts = splitSelectors(selectorString);
  for (const sel of parts) {
    try {
      await page.locator(sel).first().waitFor({ state: "visible", timeout: timeoutMs });
      return true;
    } catch {
      // try next selector
    }
  }
  return false;
}

async function ensureLoggedIn(page, plat, screenshotDir) {
  const check = plat.login_check ?? {};
  const mustBeVisible = check.must_be_visible;
  const mustBeHidden = check.must_be_hidden;
  if (!mustBeVisible && !mustBeHidden) {
    return;
  }

  let visibleOk = true;
  let hiddenOk = true;

  if (mustBeVisible) {
    visibleOk = await hasAnyVisible(page, mustBeVisible, 10000);
  }
  if (mustBeHidden) {
    hiddenOk = !(await hasAnyVisible(page, mustBeHidden, 3000));
  }

  if (visibleOk && hiddenOk) {
    return;
  }

  fs.mkdirSync(screenshotDir, { recursive: true });
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  const shot = path.join(screenshotDir, `${plat.name || "unknown"}-not-logged-in-${ts}.png`);
  await page.screenshot({ path: shot, fullPage: true }).catch(() => {});
  throw new Error(
    `login_check failed: ${plat.name || "unknown"} 似乎未登录或已过期。请先手动登录对应 user-data-dir。screenshot=${shot}`,
  );
}

async function ensureFreshAuthCookies(page, plat, screenshotDir) {
  // Enforce fresh auth state before every publish run.
  const host = String(plat.url || "").includes("toutiao.com")
    ? "https://mp.toutiao.com"
    : String(plat.url || "");
  let cookies = [];
  try {
    cookies = await page.context().cookies(host || undefined);
  } catch {
    cookies = await page.context().cookies();
  }
  const nowSec = Date.now() / 1000;
  const valid = (c) => c.expires === -1 || c.expires === 0 || c.expires > nowSec + 60;
  const authNames = new Set(["sessionid", "sid_tt", "uid_tt", "ttwid", "passport_auth_status"]);
  const authCookies = cookies.filter((c) => authNames.has(c.name));
  const hasSession = authCookies.some(
    (c) => (c.name === "sessionid" || c.name === "sid_tt") && valid(c),
  );
  if (hasSession) {
    return;
  }
  const shot = await saveFailureScreenshot(
    page,
    screenshotDir,
    plat.name || "unknown",
    "login-expired",
  );
  const cookieNames = authCookies.map((c) => c.name).join(", ") || "(none)";
  throw new Error(
    `登录态失效或缺失：未检测到有效 session cookies（found=${cookieNames}）。请先手动登录后再发布。${
      shot ? `screenshot=${shot}` : ""
    }`,
  );
}

/** Try CSS alternatives separated by " | " */
async function fillFirstVisible(page, selectorString, value, timeoutMs = 15000) {
  const parts = splitSelectors(selectorString);
  let lastErr;
  for (const sel of parts) {
    const loc = page.locator(sel).first();
    try {
      await loc.waitFor({ state: "visible", timeout: timeoutMs });
      await loc.fill(value, { timeout: 10000 });
      return;
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr ?? new Error(`No matching field for selectors: ${selectorString.slice(0, 80)}…`);
}

async function fillBody(page, selectorString, markdownText, bodyHtml, bodyPlain, timeoutMs = 15000) {
  const parts = splitSelectors(selectorString);
  let lastErr;
  for (const sel of parts) {
    const loc = page.locator(sel).first();
    try {
      await loc.waitFor({ state: "visible", timeout: timeoutMs });
      await loc.click({ timeout: 5000 });
      const pasted = await loc.evaluate((el, payload) => {
        const richHtml = String(payload.bodyHtml || "").trim();
        const plain = String(payload.bodyPlain || payload.markdownText || "");
        const tag = (el.tagName || "").toLowerCase();
        const isEditable = !!el.isContentEditable || el.getAttribute("contenteditable") === "true";

        const fire = (target) => {
          target.dispatchEvent(new Event("input", { bubbles: true }));
          target.dispatchEvent(new Event("change", { bubbles: true }));
        };

        if (isEditable) {
          el.focus();
          const selObj = window.getSelection();
          if (selObj) {
            const range = document.createRange();
            range.selectNodeContents(el);
            selObj.removeAllRanges();
            selObj.addRange(range);
          }
          document.execCommand?.("delete", false);
          try {
            const dt = new DataTransfer();
            if (richHtml) {
              dt.setData("text/html", richHtml);
            }
            dt.setData("text/plain", plain);
            let ev;
            try {
              ev = new ClipboardEvent("paste", {
                clipboardData: dt,
                bubbles: true,
                cancelable: true,
              });
            } catch {
              ev = new Event("paste", { bubbles: true, cancelable: true });
              Object.defineProperty(ev, "clipboardData", { value: dt });
            }
            el.dispatchEvent(ev);
          } catch {
            // ignore and fallback below
          }
          // fallback: paste not consumed by editor internals
          if (!String(el.textContent || "").trim()) {
            document.execCommand?.("insertText", false, plain);
          }
          fire(el);
          return true;
        }

        if (tag === "textarea" || tag === "input") {
          el.value = plain;
          fire(el);
          return true;
        }
        return false;
      }, { markdownText, bodyHtml, bodyPlain });
      if (!pasted) {
        await loc.fill(markdownText || "", { timeout: 60000 });
      }
      return;
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr ?? new Error(`No body field matched: ${selectorString.slice(0, 80)}…`);
}

/**
 * Cover upload: many sites (e.g. Toutiao pgc) keep input[type=file] hidden under a “+” tile.
 * Try setInputFiles on scoped selectors first, then click upload_trigger, then filechooser fallback.
 */
async function attachCoverFile(page, plat, coverPath, wait) {
  const selectors = plat.selectors ?? {};
  const uploadWaitSec = Number(wait?.upload ?? 3);
  const resolved = path.resolve(coverPath);
  const triggers = splitSelectors(selectors.upload_trigger || "");
  const localUploadBtns = splitSelectors(selectors.upload_local_btn || "");
  const inputSelectors = splitSelectors(selectors.upload_input || "input[type='file']");
  const modalReadySelectors = splitSelectors(selectors.upload_modal_ready || "");

  const scrollAnchor = triggers[0] || inputSelectors[0];
  if (scrollAnchor) {
    await page
      .locator(scrollAnchor)
      .first()
      .scrollIntoViewIfNeeded({ timeout: 15_000 })
      .catch(() => {});
    await sleep(200);
  }

  async function waitForUploadModal() {
    if (modalReadySelectors.length === 0) {
      return;
    }
    let lastErr;
    for (const m of modalReadySelectors) {
      try {
        await page.locator(m).first().waitFor({ state: "attached", timeout: 18_000 });
        await sleep(400);
        return;
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr ?? new Error(`upload modal not ready: ${modalReadySelectors.join(" | ")}`);
  }

  async function setOnInputs() {
    let lastErr;
    for (const sel of inputSelectors) {
      try {
        await page.locator(sel).first().setInputFiles(resolved, { timeout: 35_000 });
        return;
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr ?? new Error(`cover input not found: ${inputSelectors.join(" | ")}`);
  }

  async function uploadViaFileChooser() {
    let lastErr;
    for (const btnSel of localUploadBtns) {
      try {
        const [fc] = await Promise.all([
          page.waitForEvent("filechooser", { timeout: 15_000 }),
          page.locator(btnSel).first().click({ timeout: 10_000 }),
        ]);
        await fc.setFiles(resolved);
        await sleep(500);
        return;
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr ?? new Error(`upload local button not found: ${localUploadBtns.join(" | ")}`);
  }

  let lastErr;
  try {
    await setOnInputs();
  } catch (e) {
    lastErr = e;
    for (const trig of triggers) {
      try {
        await page.locator(trig).first().click({ timeout: 10_000 });
        await waitForUploadModal().catch(() => {});
        await sleep(500);
      } catch {
        /* try next trigger */
      }
    }
    try {
      await waitForUploadModal();
      if (localUploadBtns.length > 0) {
        await uploadViaFileChooser();
      } else {
        await setOnInputs();
      }
      lastErr = undefined;
    } catch (e2) {
      lastErr = e2;
    }
  }

  if (lastErr && triggers.length > 0) {
    try {
      const [fc] = await Promise.all([
        page.waitForEvent("filechooser", { timeout: 15_000 }),
        page.locator(triggers[0]).first().click({ timeout: 10_000 }),
      ]);
      await fc.setFiles(resolved);
      lastErr = undefined;
    } catch {
      /* keep lastErr */
    }
  }

  if (lastErr) {
    throw lastErr;
  }
  await sleep(uploadWaitSec * 1000);
}

/** Close cover upload drawer so footer buttons (e.g. 预览并发布) are clickable. */
async function dismissCoverUploadDrawer(page, plat) {
  const closers = splitSelectors(plat.selectors?.upload_modal_close || "");
  for (const sel of closers) {
    try {
      const loc = page.locator(sel).first();
      await loc.waitFor({ state: "visible", timeout: 2500 });
      await loc.click({ timeout: 4000 });
      await sleep(400);
      return;
    } catch {
      /* try next */
    }
  }
  for (let i = 0; i < 4; i++) {
    await page.keyboard.press("Escape");
    await sleep(280);
  }
}

/** Confirm selected cover inside upload drawer (e.g. Toutiao “确定”). */
async function confirmCoverUploadDrawer(page, plat) {
  const confirms = splitSelectors(
    plat.selectors?.upload_confirm_btn ||
      "button[data-e2e='imageUploadConfirm-btn'] | .confirm-btns .byte-btn-primary | .byte-drawer-footer button.byte-btn-primary | button:has-text('确定')",
  );
  for (const sel of confirms) {
    try {
      const loc = page.locator(sel).first();
      await loc.waitFor({ state: "visible", timeout: 6000 });
      await loc.click({ timeout: 10000 });
      await sleep(600);
      return true;
    } catch {
      /* try next */
    }
  }
  return false;
}

async function ensureCheckboxChecked(page, selectorString) {
  const parts = splitSelectors(selectorString);
  for (const sel of parts) {
    const root = page.locator(sel).first();
    try {
      await root.waitFor({ state: "visible", timeout: 6000 });
    } catch {
      continue;
    }
    try {
      const input = root.locator("input[type='checkbox']").first();
      const checked = await input.isChecked().catch(() => false);
      if (!checked) {
        await root.click({ timeout: 10000 });
        await sleep(250);
      }
      return true;
    } catch {
      // fallback for frameworks that toggle checked class on label
      const cls = (await root.getAttribute("class").catch(() => "")) || "";
      if (!cls.includes("byte-checkbox-checked")) {
        await root.click({ timeout: 10000 }).catch(() => {});
        await sleep(250);
      }
      return true;
    }
  }
  return false;
}

async function closeRightAssistantIfPresent(page) {
  // Toutiao right-side assistant can overlay footer area and absorb clicks.
  const closers = [
    ".creative-right-assistant [class*='close']",
    ".toutiao-creator-assistant [class*='close']",
    ".right-assistant [class*='close']",
  ];
  for (const sel of closers) {
    try {
      const loc = page.locator(sel).first();
      await loc.waitFor({ state: "visible", timeout: 1200 });
      await loc.click({ timeout: 3000 });
      await sleep(200);
      return;
    } catch {
      // try next
    }
  }
  // fallback: ESC sometimes closes floating assistant panels
  await page.keyboard.press("Escape").catch(() => {});
  await sleep(120);
}

async function clickOptionalPublishConfirm(page, plat) {
  const xpath = String(plat.selectors?.publish_confirm_xpath || "").trim();
  if (!xpath) {
    return false;
  }
  const btn = page.locator(`xpath=${xpath}`).first();
  try {
    await btn.waitFor({ state: "visible", timeout: 10_000 });
    await btn.click({ timeout: 20_000 });
    await sleep(500);
    return true;
  } catch {
    return false;
  }
}

async function waitPublishButtonReady(btn, timeoutMs = 20_000) {
  const start = Date.now();
  await btn.waitFor({ state: "visible", timeout: timeoutMs });
  while (Date.now() - start < timeoutMs) {
    const cls = (await btn.getAttribute("class").catch(() => "")) || "";
    const disabledAttr = await btn.getAttribute("disabled").catch(() => null);
    const ariaDisabled = await btn.getAttribute("aria-disabled").catch(() => null);
    const disabledByClass = /disabled|is-disabled|byte-btn-disabled/.test(cls);
    const disabled =
      disabledByClass || disabledAttr !== null || String(ariaDisabled).toLowerCase() === "true";
    if (!disabled) {
      return;
    }
    await sleep(120);
  }
  throw new Error("发布按钮未进入可点击状态（仍为 disabled）");
}

async function waitPreviewReady(page, plat) {
  const sels = splitSelectors(
    plat.selectors?.preview_ready ||
      "iframe, .byte-modal, .byte-drawer, .preview, [class*='preview'], [class*='dialog']",
  );
  const timeoutMs = Number(plat.selectors?.preview_wait_ms || 12000);
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    for (const sel of sels) {
      try {
        const loc = page.locator(sel).first();
        await loc.waitFor({ state: "visible", timeout: 600 });
        return true;
      } catch {
        // try next
      }
    }
    await sleep(120);
  }
  return false;
}

async function readInlinePublishHint(page, plat) {
  const selectors = splitSelectors(
    plat.selectors?.publish_hint ||
      ".hint-warn-tip, .byte-message-error, .byte-notice-content, .byte-alert-content, .publish-editor-title-wrapper .hint-warn-tip",
  );
  for (const sel of selectors) {
    try {
      const txt = (await page.locator(sel).first().innerText({ timeout: 500 })).trim();
      if (txt) {
        return txt;
      }
    } catch {
      // try next
    }
  }
  return "";
}

async function readTransientErrTip(page, maxMs = 1600) {
  const endAt = Date.now() + maxMs;
  while (Date.now() < endAt) {
    try {
      const txt = await page.evaluate(() => {
        const nodes = Array.from(document.querySelectorAll("p[class^='err-tip']"));
        const text = nodes
          .map((n) => (n.textContent || "").trim())
          .filter(Boolean)
          .join(" | ");
        return text;
      });
      if (txt) {
        return txt;
      }
    } catch {
      // ignore transient evaluation failures
    }
    await sleep(100);
  }
  return "";
}

async function clickPublishWithVerification(page, btn, plat) {
  const beforeCls = (await btn.getAttribute("class").catch(() => "")) || "";
  await btn.click({ timeout: 30_000 });
  await sleep(350);
  const afterCls = (await btn.getAttribute("class").catch(() => "")) || "";
  const classChanged = beforeCls !== afterCls;
  const looksBusy =
    /loading|disabled|is-loading|is-disabled|byte-btn-disabled/.test(afterCls) ||
    /loading|disabled|is-loading|is-disabled|byte-btn-disabled/.test(beforeCls);
  if (classChanged || looksBusy) {
    return;
  }
  // If plain click causes no visible state change, fallback to JS dispatch + force click.
  await btn.dispatchEvent("click").catch(() => {});
  await sleep(250);
  try {
    await btn.click({ timeout: 10_000, force: true });
  } catch {
    // ignore and let later outcome checks decide
  }
  await sleep(300);
  const transient = await readTransientErrTip(page, 1600);
  if (transient) {
    throw new Error(`发布点击后提示: ${transient}`);
  }
  const hint = await readInlinePublishHint(page, plat);
  if (hint) {
    throw new Error(`发布点击后提示: ${hint}`);
  }
}

async function waitPublishOutcome(page, plat, startUrl) {
  const successSelectors = splitSelectors(plat.selectors?.publish_success || "");
  const errorSelectors = splitSelectors(
    plat.selectors?.publish_error ||
      ".byte-message-error, .byte-notice-content, .byte-alert-content, .hint-warn-tip, .publish-editor-title-wrapper .hint-warn-tip",
  );
  const successUrl = String(plat.selectors?.publish_success_url || "").trim();
  const deadline = Date.now() + 25_000;
  while (Date.now() < deadline) {
    // 1) success toast / banner
    for (const sel of successSelectors) {
      try {
        await page.locator(sel).first().waitFor({ state: "visible", timeout: 800 });
        return;
      } catch {
        /* try next */
      }
    }
    // 2) url moved away from publish page
    const cur = page.url();
    if (successUrl && cur.startsWith(successUrl)) {
      return;
    }
    if (cur && startUrl && cur !== startUrl) {
      return;
    }
    // 3) common validation / error tips
    const transient = await readTransientErrTip(page, 300);
    if (transient) {
      throw new Error(`发布失败提示: ${transient}`);
    }
    for (const sel of errorSelectors) {
      try {
        const txt = (await page.locator(sel).first().innerText({ timeout: 400 })).trim();
        if (txt) {
          // Some pages emit autosave warnings like "保存失败" that are not publish failure.
          if (txt.includes("保存失败")) {
            continue;
          }
          const publishRelated =
            txt.includes("发布") ||
            txt.includes("请完善") ||
            txt.includes("请输入") ||
            txt.includes("标题") ||
            txt.includes("正文") ||
            txt.includes("封面");
          if (publishRelated) {
            throw new Error(`发布失败提示: ${txt}`);
          }
        }
      } catch (e) {
        if (e instanceof Error && e.message.startsWith("发布失败提示:")) {
          throw e;
        }
      }
    }
    await sleep(400);
  }
  throw new Error(
    `发布结果未确认：未命中成功提示，且 URL 未变化（start=${startUrl}, now=${page.url()}）`,
  );
}

function capturePublishApiResponses(page) {
  const records = [];
  const onResp = async (resp) => {
    try {
      const req = resp.request();
      const method = req.method();
      const resourceType = req.resourceType();
      const url = resp.url();
      const looksApi = /fetch|xhr/i.test(resourceType);
      const looksWrite = /POST|PUT|PATCH/i.test(method);
      const looksRelated =
        /publish|article|save|draft|graphic|content|_signature|a_bogus|mp\.toutiao|toutiao|pgc/i.test(
          url,
        );
      if (!((looksApi && looksWrite) || looksRelated)) {
        return;
      }
      const status = resp.status();
      let body = "";
      try {
        body = (await resp.text()).slice(0, 500);
      } catch {
        body = "";
      }
      let postData = "";
      try {
        postData = (req.postData() || "").slice(0, 800);
      } catch {
        postData = "";
      }
      records.push({ method, status, resourceType, url, postData, body });
      if (records.length > 80) {
        records.shift();
      }
    } catch {
      // ignore capture failures
    }
  };
  page.on("response", onResp);
  return {
    snapshot() {
      return records.slice();
    },
    stop() {
      page.off("response", onResp);
      return records.slice();
    },
  };
}

async function runPlatform(page, plat, article, coverPath, wait, screenshotDir, publishLogPath) {
  const platName = plat.name ?? "unknown";
  const selectors = plat.selectors ?? {};
  const pageWaitSec = Number(wait?.page ?? 4);

  publishStepLog(publishLogPath, `${platName}: goto ${plat.url}`);
  await page.goto(plat.url, { waitUntil: "domcontentloaded", timeout: 90_000 });
  publishStepLog(publishLogPath, `${platName}: goto done url=${page.url()}`);
  await sleep(pageWaitSec * 1000);
  publishStepLog(publishLogPath, `${platName}: login_check + cookie check`);
  await ensureLoggedIn(page, plat, screenshotDir);
  await ensureFreshAuthCookies(page, plat, screenshotDir);
  publishStepLog(publishLogPath, `${platName}: auth ok`);

  const titleSel = selectors.title || "input";
  publishStepLog(publishLogPath, `${platName}: fill title`);
  await fillFirstVisible(page, titleSel, article.title ?? "");
  await sleep(400);

  const bodySel = selectors.body || "[contenteditable='true']";
  publishStepLog(publishLogPath, `${platName}: fill body`);
  await fillBody(
    page,
    bodySel,
    article.body ?? "",
    article.body_html ?? "",
    article.body_plain ?? "",
  );
  await sleep(400);

  if (coverPath && fs.existsSync(coverPath)) {
    publishStepLog(publishLogPath, `${platName}: cover attach ${coverPath}`);
    await attachCoverFile(page, plat, coverPath, wait);
    publishStepLog(publishLogPath, `${platName}: cover confirm drawer`);
    await confirmCoverUploadDrawer(page, plat);
    publishStepLog(publishLogPath, `${platName}: cover dismiss drawer`);
    await dismissCoverUploadDrawer(page, plat);
  } else {
    publishStepLog(publishLogPath, `${platName}: cover skipped (no path or missing file)`);
  }

  // Toutiao: user expects "引用AI" selected before publish.
  if (selectors.ai_quote_checkbox) {
    publishStepLog(publishLogPath, `${platName}: ensure AI quote checkbox`);
    await ensureCheckboxChecked(page, selectors.ai_quote_checkbox);
  }
  publishStepLog(publishLogPath, `${platName}: close right assistant if any`);
  await closeRightAssistantIfPresent(page);

  const capture = capturePublishApiResponses(page);
  const startUrl = page.url();
  const beforeSnapshot = capture.snapshot();
  const xpath = selectors.publish_btn_xpath || "//button[contains(.,'发布')]";
  const btn = page.locator(`xpath=${xpath}`).first();
  publishStepLog(publishLogPath, `${platName}: wait primary publish button`);
  await waitPublishButtonReady(btn, 25_000);
  try {
    publishStepLog(publishLogPath, `${platName}: click primary publish`);
    await clickPublishWithVerification(page, btn, plat);
  } catch {
    publishStepLog(publishLogPath, `${platName}: primary publish retry after dismiss drawer`);
    await dismissCoverUploadDrawer(page, plat);
    await waitPublishButtonReady(btn, 15_000);
    await clickPublishWithVerification(page, btn, plat);
  }
  publishStepLog(publishLogPath, `${platName}: wait preview layer`);
  await waitPreviewReady(page, plat);
  publishStepLog(publishLogPath, `${platName}: optional confirm publish`);
  await clickOptionalPublishConfirm(page, plat);
  try {
    publishStepLog(publishLogPath, `${platName}: wait publish outcome`);
    await waitPublishOutcome(page, plat, startUrl);
    publishStepLog(publishLogPath, `${platName}: publish outcome ok url=${page.url()}`);
  } catch (e) {
    const all = capture.stop();
    const fresh = all.slice(beforeSnapshot.length);
    const submitHits = fresh.filter((x) =>
      /publish|article|save|draft|graphic|content|mp\.toutiao|toutiao|pgc/i.test(x.url),
    );
    const tail = fresh.slice(-8);
    const extra = `; publish_api_new_count=${fresh.length}; submit_hits=${submitHits.length}; publish_api_tail=${JSON.stringify(
      tail,
    )}`;
    publishStepLog(
      publishLogPath,
      `${platName}: publish outcome error ${e instanceof Error ? e.message : String(e)}`,
    );
    if (e instanceof Error) {
      throw new Error(`${e.message}${extra}`);
    }
    throw e;
  }
  capture.stop();
  await sleep(1200);
}

async function main() {
  const payloadPath = process.argv[2];
  if (!payloadPath) {
    console.error("Usage: node publish_playwright.mjs <payload.json>");
    process.exit(2);
  }
  const raw = fs.readFileSync(payloadPath, "utf8");
  const payload = JSON.parse(raw);
  const platforms = payload.platforms ?? [];
  const article = payload.article ?? {};
  const coverPath = payload.coverPath || null;
  const wait = payload.wait ?? {};
  const headless =
    payload.headless === true ||
    ["1", "true", "yes"].includes(String(process.env.PLAYWRIGHT_HEADLESS || "").toLowerCase());
  const publishLogPath = payload.publishLogPath || "";
  publishStepLog(
    publishLogPath,
    `script start headless=${headless} payload=${path.basename(payloadPath)}`,
  );
  const payloadDir = path.dirname(path.resolve(payloadPath));
  let screenshotDir = payload.screenshotDir || path.join(payloadDir, "login-failures");
  if (!path.isAbsolute(screenshotDir)) {
    screenshotDir = path.join(payloadDir, screenshotDir);
  }

  const chromeArgs = ["--disable-blink-features=AutomationControlled"];
  const executablePath = process.env.CHROME_BINARY?.trim() || undefined;

  const results = [];

  for (const plat of platforms) {
    const name = plat.name ?? "unknown";
    const userDataDir = plat.chrome_user_data_dir;
    if (!userDataDir) {
      publishStepLog(publishLogPath, `${name}: missing chrome_user_data_dir`);
      results.push({ platform: name, success: false, error: "missing chrome_user_data_dir" });
      continue;
    }
    const dir = path.resolve(userDataDir.replace(/^~(?=\/)/, process.env.HOME || ""));
    const lockInfo = inspectSingletonLock(dir);
    if (lockInfo) {
      publishStepLog(publishLogPath, `${name}: abort SingletonLock ${lockInfo.lockPath}`);
      const who =
        lockInfo.matching.length > 0
          ? `检测到占用进程：${lockInfo.matching.slice(0, 3).join(" | ")}`
          : "未识别到占用进程，可能是残留锁文件";
      results.push({
        platform: name,
        success: false,
        error:
          `启动前检查失败：发现浏览器 profile 锁文件 ${lockInfo.lockPath}。${who}。` +
          "请先关闭使用该 profile 的 Chrome/Playwright 后重试；若确认没有进程占用，可手动删除该锁文件。",
      });
      continue;
    }

    const launchOpts = {
      headless,
      channel: executablePath ? undefined : "chrome",
      executablePath: executablePath || undefined,
      args: chromeArgs,
      viewport: { width: 1360, height: 900 },
    };

    let context;
    let page;
    try {
      publishStepLog(publishLogPath, `${name}: launchPersistentContext userDataDir=${dir}`);
      fs.mkdirSync(dir, { recursive: true });
      context = await chromium.launchPersistentContext(dir, launchOpts);
      page = context.pages()[0] ?? (await context.newPage());
      publishStepLog(publishLogPath, `${name}: browser page ready`);
      await runPlatform(page, plat, article, coverPath, wait, screenshotDir, publishLogPath);
      publishStepLog(publishLogPath, `${name}: runPlatform finished ok`);
      results.push({ platform: name, success: true });
    } catch (err) {
      publishStepLog(
        publishLogPath,
        `${name}: error ${err instanceof Error ? err.message : String(err)}`,
      );
      const shot = await saveFailureScreenshot(page, screenshotDir, name, "error");
      let msg = err instanceof Error ? err.message : String(err);
      if (shot) {
        msg = `${msg}\nscreenshot=${shot}`;
      }
      console.error(`[${name}]`, msg);
      results.push({ platform: name, success: false, error: msg });
    } finally {
      if (context) {
        await Promise.race([
          context.close(),
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error("context.close timeout")), 45_000),
          ),
        ]).catch(() => {});
      }
    }
  }

  const outPath = payloadPath.replace(/\.json$/i, ".result.json");
  publishStepLog(
    publishLogPath,
    `script end ok=${results.every((r) => r.success)} results=${JSON.stringify(results.map((r) => ({ platform: r.platform, success: r.success })))}`,
  );
  fs.writeFileSync(
    outPath,
    JSON.stringify({ ok: results.every((r) => r.success), results }, null, 2),
    "utf8",
  );
  if (!results.every((r) => r.success)) {
    process.exit(1);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});

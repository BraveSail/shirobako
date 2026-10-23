/**
 * 生成 content-bound PO Token（YouTube SABR 的 attestation 用）。
 *
 * 背景（2026-10-22）：YouTube 对 SABR 下发 ``STREAM_PROTECTION_STATUS = 3``，
 * googlevideo 抛 ``Cannot proceed with stream: attestation required`` —— 必须先给
 * ``SabrStream.setPoToken()`` 一个绑定该视频的 PO token 才能取流。
 *
 * 用法:
 *   bun gen-pot.mjs <videoId>
 *   → stdout 一行 JSON: {"poToken": "...", "expiresAt": "2026-10-23T20:16:19.215Z"}
 *
 * 流程（与 bgutil-ytdlp-pot-provider 的 server 同一套公开 API，不复制它的代码）:
 *   1. GET https://www.youtube.com → 提取 ``ytcfg.set({…})``（EVENT_ID 等）+ ``window.ytAtN({…})``
 *      （bgChallenge：program + interpreterUrl）
 *   2. 加载 interpreter JS（BotGuard VM）→ ``BotGuardClient.snapshot()`` 拿 botguardResponse
 *   3. ``POST att/get``（GenerateIT）→ integrityToken
 *   4. ``WebPoMinter.create(…)`` → ``mintAsWebsafeString(videoId)`` → poToken
 *
 * 失败一律抛错（非零退出 + stderr），**不静默** —— 调用方要能看到原因。
 */
import { BotGuardClient } from 'bgutils-js/botguard';
import { WebPoMinter } from 'bgutils-js/webpo';
import { buildURL, getHeaders, parseLooseJSON } from 'bgutils-js/utils';
import { JSDOM } from 'jsdom';

// 与取流侧 / 无头刷新同一族（三处 UA 必须一致，见 test_youtube_ua_consistency）
const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36';
const REQUEST_KEY = 'O43z0dpjhgX20SCx4KAo';

/** 建立一个浏览器样的全局环境（BotGuard 会读写 window/document/navigator）。 */
function setupDom() {
  if (globalThis.window && globalThis.document) return;
  const dom = new JSDOM(
    '<!DOCTYPE html><html lang="en"><head><title></title></head><body></body></html>',
    { url: 'https://www.youtube.com/', referrer: 'https://www.youtube.com/', userAgent: UA }
  );
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    location: dom.window.location,
    origin: dom.window.origin,
  });
  if (!Reflect.has(globalThis, 'navigator')) {
    Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator });
  }
}

/** 从 YouTube 首页拿自洽的 (ytcfg, bgChallenge) —— 缺 EVENT_ID 的 challenge 会被 att/get 拒。 */
async function fetchChallenge() {
  const resp = await fetch('https://www.youtube.com', {
    headers: { accept: '*/*', 'accept-language': 'en-US,en;q=0.7', 'user-agent': UA },
  });
  const html = await resp.text();

  const ytcfg = html.match(/ytcfg\.set\(({.+?})\);/s);
  if (!ytcfg) throw new Error('首页没有 ytcfg（EVENT_ID 缺失）');
  const ytObj = { config_: parseLooseJSON(ytcfg[1]) };
  globalThis.yt = ytObj;
  if (globalThis.window) globalThis.window.yt = ytObj;

  const att = html.match(/window\.ytAtN\(\s*({[\s\S]*?})\s*\)/);
  if (!att) throw new Error('首页没有 ytAtN challenge');
  // ytAtN 里是 JS 对象字面量（单引号/裸 undefined）—— 必须用宽松解析器
  const bgChallenge = parseLooseJSON(att[1])?.R?.bgChallenge;
  if (!bgChallenge?.program || !bgChallenge?.interpreterUrl) {
    throw new Error('ytAtN 里没有 bgChallenge（program/interpreterUrl）');
  }
  return bgChallenge;
}

export async function generatePoToken(videoId) {
  setupDom();
  const bg = await fetchChallenge();

  const interpreterUrl = bg.interpreterUrl.privateDoNotAccessOrElseTrustedResourceUrlWrappedValue;
  const interpreterJs = await (await fetch(`https:${interpreterUrl}`)).text();
  new Function(interpreterJs)(); // 加载 BotGuard VM

  const bgClient = await BotGuardClient.create({
    program: bg.program,
    globalName: bg.globalName,
    globalObject: globalThis,
  });

  const webPoSignalOutput = [];
  const botguardResponse = await bgClient.snapshot({ webPoSignalOutput });

  const itResp = await fetch(buildURL('GenerateIT'), {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify([REQUEST_KEY, botguardResponse]),
  });
  const [integrityToken, estimatedTtlSecs, mintRefreshThreshold, websafeFallbackToken] =
    await itResp.json();
  if (!integrityToken) throw new Error(`att/get 没给 integrity token: ${await itResp.text()}`);

  const minter = await WebPoMinter.create(
    { integrityToken, estimatedTtlSecs, mintRefreshThreshold, websafeFallbackToken },
    webPoSignalOutput
  );
  const poToken = await minter.mintAsWebsafeString(videoId);
  const expiresAt = new Date(Date.now() + (estimatedTtlSecs || 21600) * 1000).toISOString();
  return { poToken, expiresAt };
}

// CLI 入口（被 import 时不执行）
if (import.meta.main) {
  const videoId = (process.argv[2] || '').trim();
  if (!videoId) {
    console.error('用法: bun gen-pot.mjs <videoId>');
    process.exit(2);
  }

  try {
    const t0 = Date.now();
    const result = await generatePoToken(videoId);
    console.error(`[gen-pot] ${videoId} 生成完成 (${Date.now() - t0}ms, len=${result.poToken.length})`);
    console.log(JSON.stringify(result));
  } catch (e) {
    console.error(`[gen-pot] 失败: ${e?.stack || e}`);
    process.exit(1);
  }
}

/**
 * YouTube SABR 取流工 —— 用 LuanRT/googlevideo 的 SabrStream 把高清流拉到本地文件。
 *
 * 用法:
 *   bun sabr.mjs --input /tmp/in.json --outdir /tmp/out
 *
 * input JSON 字段:
 *   videoId                    (必填)
 *   serverAbrStreamingUrl      (必填, **n 必须已解**)
 *   videoPlaybackUstreamerConfig (必填, base64)
 *   adaptiveFormats            (必填, YouTube 原始数组 —— buildSabrFormat 兼容 snake/camel)
 *   cookie                     (必填, "k=v; k=v" 字符串)
 *   videoQuality               (可选, 默认 '1080p')
 *
 * 输出 (stdout 最后一行 JSON):
 *   {"ok": true, "videoPath": ..., "audioPath": ..., "videoItag": ..., "audioItag": ...,
 *    "videoBytes": ..., "audioBytes": ...}
 *   失败: {"ok": false, "error": "..."}  + 退出码 1
 */
import { SabrStream } from 'googlevideo/sabr-stream';
import { buildSabrFormat, EnabledTrackTypes } from 'googlevideo/utils';
import { existsSync, readFileSync, mkdirSync, openSync, writeSync, closeSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const args = {};
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i].replace(/^--/, '');
  args[key] = process.argv[i + 1];
}

const inputPath = args.input;
const outdir = args.outdir || '/tmp/yt-sabr-out';
if (!inputPath) fail('missing --input');

/**
 * 输出一行 JSON 后**等 stdout flush 再退出**。
 *
 * ⚠️ 不能写完就 `process.exit()`：stdout 是管道（Python 子进程就是）时它是**异步**的，
 * exit 会把还没 flush 的数据丢掉 —— 现象是「退出码 1、stderr 空、没有任何错误信息」，
 * 排查时完全瞎（本项目实际踩过）。
 */
function exitAfterFlush(code) {
  const done = () => process.exit(code);
  if (process.stdout.writableLength === 0) done();
  else process.stdout.write('', done);
}

function fail(msg) {
  console.log(JSON.stringify({ ok: false, error: String(msg) }));
  exitAfterFlush(1);
}

let input;
try {
  input = JSON.parse(readFileSync(inputPath, 'utf8'));
} catch (e) {
  fail(`bad input json: ${e.message}`);
}

const { videoId, serverAbrStreamingUrl, videoPlaybackUstreamerConfig, adaptiveFormats, cookie } = input;
if (!videoId || !serverAbrStreamingUrl || !videoPlaybackUstreamerConfig || !adaptiveFormats?.length) {
  fail('missing required fields (videoId/serverAbrStreamingUrl/videoPlaybackUstreamerConfig/adaptiveFormats)');
}

mkdirSync(outdir, { recursive: true });
// 扩展名按**实际选中的容器**定（AVC/AV1→mp4, VP9→webm）—— 写死 .mp4 会撒谎，
// 下游 ffmpeg mux / TG 发送都按扩展名判断，撒谎会炸。
// 这里先占位，选出格式后在下方改真实的（见 vExt/aExt）。
let videoPath = join(outdir, 'video.mp4');
let audioPath = join(outdir, 'audio.m4a');

const formats = adaptiveFormats.map(buildSabrFormat);
const maxHeight = Number(input.maxHeight || 0) || parseInt(input.videoQuality || '1080p', 10) || 1080;
console.error(`[sabr] formats=${formats.length} quality=${input.videoQuality || '1080p'} maxHeight=${maxHeight}`);

// 显式指定的 itag（可选）—— 比编码偏好更直接可控:
//   videoItag: 137=avc/mp4, 248=vp9/webm, 399=av1/mp4 (1080p 三档)
//   audioItag: 140=aac/mp4, 251=opus/webm
// 不指定则走 codecPriority（下）。
const videoItag = input.videoItag ? Number(input.videoItag) : undefined;
const audioItag = input.audioItag ? Number(input.audioItag) : undefined;
if (videoItag) console.error(`[sabr] 显式指定 video itag=${videoItag}`);
if (audioItag) console.error(`[sabr] 显式指定 audio itag=${audioItag}`);

/**
 * 编码优先级选流（可选）。
 *
 * 值形如 ``['av1','vp9','avc']``。库自带的 ``chooseFormat`` 只有
 * preferH264/preferWebM/preferMP4 三个布尔偏好、且同分辨率下**按数组顺序**取
 * （等于 AVC 优先），拿不到「AV1 > VP9 > AVC」这种次序。
 *
 * ★★ **分辨率优先，同分辨率内才比编码**（2026-10-16 修正 —— 这是必须的）:
 *
 * 编码优先级**不能跨分辨率**用。实测反例（`_IBAJkEsuG4`）:
 *   avc1: 144p / 360p / 720p / **1080p**   ← 高清只有 AVC
 *   vp9:  只有 **360p**                     ← VP9 只有低清
 *   av01: 无
 * 若「按编码优先」先找 vp9，会命中它的 360p 并**降到 360p**（而 AVC 有 1080p）。
 * 正确做法: 先取素材里可用的**最高分辨率**，在该档内按 codecPriority 挑编码；
 * 该档没有偏好编码就取这一档里码率最高的（而不是掉到低分辨率的偏好编码）。
 *
 * 选中的编码决定音频：av1/avc → mp4 容器（配 aac itag=140）；
 * vp9 → webm 容器（配 opus itag=251）—— 同一编码族的容器要一致。
 */
const CODEC_PATTERNS = {
  av01: 'av01',
  av1: 'av01', // 用户/文档常写 av1，素材里是 av01 —— 两个键都要
  vp9: 'vp9',
  avc: 'avc1',
  h264: 'avc1',
};
const CODEC_AUDIO = {
  av01: { container: 'mp4', audio: 'mp4a' },
  av1: { container: 'mp4', audio: 'mp4a' },
  vp9: { container: 'webm', audio: 'opus' },
  avc: { container: 'mp4', audio: 'mp4a' },
  h264: { container: 'mp4', audio: 'mp4a' },
};

function pickByCodecPriority(priority) {
  const wanted = priority.map((c) => String(c).toLowerCase());
  const videos = formats.filter(
    (f) => f.mimeType?.startsWith('video/') && (f.height || 0) > 0 && (f.height || 0) <= maxHeight
  );
  if (!videos.length) return null;

  // ① 最高分辨率档（分辨率优先 —— 不能为了编码牺牲画质）
  const topHeight = Math.max(...videos.map((f) => f.height || 0));
  const topTier = videos.filter((f) => (f.height || 0) === topHeight);

  // ② 该档内按 codecPriority 挑；都没有就取这一档码率最高的
  let video = null;
  let codec = '';
  for (const want of wanted) {
    const pattern = CODEC_PATTERNS[want];
    if (!pattern) continue;
    const hit = topTier
      .filter((f) => f.mimeType?.includes(pattern))
      .sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0))[0];
    if (hit) {
      video = hit;
      codec = want;
      break;
    }
  }
  if (!video) {
    video = topTier.sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0))[0];
    codec = Object.keys(CODEC_PATTERNS).find((k) => video.mimeType?.includes(CODEC_PATTERNS[k])) || '';
  }

  // ③ 音频: 按选中视频的容器族配（vp9→opus, 其它→aac），默认音轨（空 xtags）优先
  const spec = CODEC_AUDIO[codec];
  const acodec = spec?.audio || 'mp4a';
  const audios = formats
    .filter((f) => f.mimeType?.startsWith('audio/') && f.mimeType.includes(acodec))
    .sort((a, b) => {
      const aDefault = a.xtags ? 1 : 0;
      const bDefault = b.xtags ? 1 : 0;
      return aDefault - bDefault || (b.bitrate || 0) - (a.bitrate || 0);
    });
  if (!audios.length) return null;
  return { video, audio: audios[0], codec };
}

// ★ **UA 单一真源** —— 与无头刷新脚本（``scripts/yt-cookie/refresh_cookie.py`` 的 ``UA``）
// 和取流侧（``youtube_video.py`` 的 ``_WEB_USER_AGENT`` / ``_WATCH_USER_AGENT``）
// **必须逐字一致**。三个环节在跟同一个账号会话打交道，UA 不一致会露馅。
//
// **取值依据 = curl_cffi 实际发出的 UA**（项目取流主路用它）: ``http.IMPERSONATE`` 是
// ``chrome150`` ⇒ TLS 指纹伪装成 Chrome 150（macOS），UA 必须跟它匹配。实测回显:
// ``https://httpbin.org/user-agent``。
//
// ⚠️ 不设的话 bun 的 fetch 会发 ``Bun/1.4.3`` —— 完全不是浏览器，直球暴露自动化。
// 改这里要同时改另外两处（``test_youtube_ua_consistency.py`` 钉住三处一致）。
const DEFAULT_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36';
const UA = input.userAgent || DEFAULT_UA;
console.error(`[sabr] UA: ${UA.slice(0, 60)}...`);

// 自定义 fetch: 补 cookie + UA（SabrStream 自己管 headers/body）
const fetchWithCookie = (url, options = {}) => {
  const headers = new Headers(options.headers || {});
  if (cookie) headers.set('cookie', cookie);
  if (!headers.has('user-agent')) headers.set('user-agent', UA);
  return fetch(url, { ...options, headers });
};

//: 直播默认抓多久 —— 从**当前时刻（直播边缘）**往前抓这么多秒。
//: 12s ≈ 1080p 10MB 上下，够看清内容又不至于让用户等太久。
const LIVE_DEFAULT_SECONDS = 30;

let vb = 0, ab = 0;
//: 直播是我们主动停的 —— abort 会走 error 通道，要区分「预期收尾」和「真失败」
let liveStopped = false;

try {
  // ★ PO Token（attestation）—— 2026-10-22 起 YouTube 对 SABR 返回
  // ``STREAM_PROTECTION_STATUS status=3``（"attestation required"），必须先注入
  // 一个 content-bound 的 PO token 才能取流（见 ``gen-pot.mjs``）。
  // 必须在 ``start()`` **之前**设置 —— 它是首帧请求体的一部分。
  // 调用方没给就**就地生成**（~1–4s）；生成失败时打印原因并继续（让服务端的
  // 真实错误暴露出来，而不是被一个笼统的"没 token"盖掉）。
  let poToken = (input.poToken || '').trim();
  // 跨进程缓存：同一 videoId 的 token 有效期 ~6h，重试/重发时不必再生成
  const potCacheDir = '/tmp/yt-pot-cache';
  const potCacheFile = join(potCacheDir, `${videoId}.json`);
  if (!poToken) {
    try {
      if (existsSync(potCacheFile)) {
        const cached = JSON.parse(readFileSync(potCacheFile, 'utf8'));
        // 留 1 分钟余量，避免刚好过期
        if (cached.poToken && new Date(cached.expiresAt).getTime() > Date.now() + 60_000) {
          poToken = cached.poToken;
          console.error(`[sabr] 复用缓存的 PO token (len=${poToken.length})`);
        }
      }
    } catch {
      /* 缓存坏了就当没有 */
    }
  }
  if (!poToken) {
    try {
      const t0 = Date.now();
      const { generatePoToken } = await import('./gen-pot.mjs');
      const generated = await generatePoToken(videoId);
      poToken = generated.poToken;
      console.error(`[sabr] 自动生成 PO token (len=${poToken.length}, ${Date.now() - t0}ms)`);
      try {
        mkdirSync(potCacheDir, { recursive: true });
        writeFileSync(potCacheFile, JSON.stringify(generated));
      } catch {
        /* 缓存写失败不影响取流 */
      }
    } catch (e) {
      console.error(`[sabr] PO token 生成失败: ${e?.message || e}`);
    }
  }
  console.error(
    poToken ? `[sabr] PO token 已注入 config (len=${poToken.length})` : '[sabr] 没有可用 PO token'
  );

  // ★ googlevideo 5.x: ``fetchFunction``（旧版是 ``fetch``）；PO token 走 config
  // 而不是 setPoToken()；``durationMs`` / ``startTimeMs`` 是 **start() 的参数**。
  const stream = new SabrStream({
    // ⚠️ 5.x 的 ``SabrStreamConfig`` **必填 videoId** —— 心跳请求和 broadcast 校验都用它。
    // 4.x 不需要所以以前没传；缺了之后直播（isBroadcast）一条数据都取不到，且不报错，
    // 只是流一直空着。
    videoId,
    fetchFunction: fetchWithCookie,
    serverAbrStreamingUrl,
    videoPlaybackUstreamerConfig,
    formats,
    clientInfo: { clientName: 1, clientVersion: '2.20261015.01.00' },
    poToken: poToken || undefined,
  });

  let streamError = null;
  stream.on('error', (e) => {
    const msg = e?.message || String(e);
    // 直播到点是**我们自己 abort** 的: 库会以 'Stream aborted' 走 error 通道,
    // 那是正常收尾, 不是失败（否则每次直播都会被判成下载失败）。
    if (liveStopped && /abort/i.test(msg)) {
      console.error(`[sabr] 直播已停止（预期）: ${msg}`);
      return;
    }
    streamError = msg;
    console.error(`[sabr] stream error: ${streamError}`);
  });

  // 显式 itag > codecPriority > 库默认
  let videoSpec = videoItag;
  let audioSpec = audioItag;
  let pickedCodec = '';
  if (!videoSpec && !audioSpec && Array.isArray(input.codecPriority) && input.codecPriority.length) {
    const picked = pickByCodecPriority(input.codecPriority);
    if (picked) {
      videoSpec = picked.video;
      audioSpec = picked.audio;
      pickedCodec = picked.codec;
      console.error(
        `[sabr] codecPriority=${input.codecPriority.join('>')} → ${picked.codec} ` +
          `(video itag=${picked.video.itag} ${picked.video.mimeType}, audio itag=${picked.audio.itag})`
      );
    } else {
      console.error(`[sabr] codecPriority=${input.codecPriority.join('>')} 在素材里都找不到, 回退库默认`);
    }
  }

  // ⚠️ **必须给 durationMs**: 不给时库按 Infinity 跑（直播/长视频），要下多久没有上限。
  // 直播是特例: 它的 ``mediaHeader.contentLength`` 恒为 0、段长也是 0，4.1.1 会把每段
  // 当成"长度不符"整段丢弃 —— 所以 5.x 才有 ``isBroadcast`` / ``startTimeMs`` /
  // 心跳 这一整套。直播按 ``input.liveSeconds`` 从**直播边缘**往前抓。
  //
  // 素材自带 approxDurationMs（点播的 SABR 格式一般有），取最大值即可。
  const contentDurationMs =
    Number(input.durationMs || 0) ||
    (() => {
      const ds = (adaptiveFormats || [])
        .map((f) => Number(f.approxDurationMs || f.durationMs || 0))
        .filter((n) => n > 0);
      return ds.length ? Math.max(...ds) : 0;
    })();
  // ★ **直播不能用 durationMs 当停止条件**。直播的 ``playerTimeMs`` 从**直播边缘**
  // 开始（实测 headTimeMs ≈ 5.6e6 且持续增长），库里 ``while (playerTimeMs < durationMs)``
  // 在 durationMs=10000 时**第一个循环就不成立**、或永远成立 —— 两种都不是我们要的。
  // 正确做法: 不管时长，抓到**内容时长够 liveSeconds** 就 ``abort()``。
  // 内容时长用库暴露的 ``videoEndTimeMs``（已下载到的媒体时间）来量。
  const liveSeconds = Number(input.liveSeconds || 0) || LIVE_DEFAULT_SECONDS;
  const isLive = !!input.isLive;
  const liveTargetMs = isLive && liveSeconds > 0 ? liveSeconds * 1000 : 0;
  const durationMs = isLive ? undefined : contentDurationMs || undefined;
  if (isLive) {
    console.error(
      `[sabr] 直播: 从当前时刻（直播边缘）起抓 ${liveSeconds || '∞'} 秒 — 墙钟计时, ${(Number(input.liveGraceSeconds ?? 6))}s readahead 余量`
    );
  }

  // ★ 5.x 的选流参数: ``videoFormat`` / ``audioFormat``（itag 或 SabrFormat 对象）
  // 或 ``videoPreferences`` {quality, container, preferredVideoCodec}。
  // 旧版的顶层 ``videoQuality`` 已经不存在了。
  const { videoStream, audioStream, selectedFormats } = await stream.start({
    videoFormat: videoSpec || undefined,
    audioFormat: audioSpec || undefined,
    // ⚠️ 5.x 强制要求 **video 和 audio 两侧都**给出 spec 或 preferences
    // （``selectFormats`` 里 assert 两个都要有），只给视频侧会直接报
    // "Expected both video and audio format or preferences to be provided"。
    videoPreferences: videoSpec
      ? undefined
      : { quality: input.videoQuality || '1080p' },
    // 不写 quality: 直播的音轨只有 MEDIUM（itag 140），指定 HIGH 会一条都匹配不到。
    // 只给编码偏好，让库在可用音轨里挑。
    audioPreferences: audioSpec ? undefined : { preferredAudioCodec: 'aac' },
    enabledTrackTypes: EnabledTrackTypes.VIDEO_AND_AUDIO,
    maxRetries: 3,
    stallDetectionMs: 30000,
    durationMs,
    // 直播: **不传** startTimeMs —— 5.x 自己用 LIVE_EDGE_SENTINEL_MS 从直播边缘开始；
    // 传 0 会从"最开始"抓，直播没有最开始（会一直等）。抓够 liveSeconds 就停。
    isPostLiveDvr: !!input.isPostLiveDvr,
  });

  console.error(`[sabr] selected video=${selectedFormats.videoFormat?.itag} audio=${selectedFormats.audioFormat?.itag}`);

  // 按**选中格式的 mimeType** 定扩展名（vp9 的 webm 不能用 .mp4 撒谎）
  const vMime = selectedFormats.videoFormat?.mimeType || '';
  const aMime = selectedFormats.audioFormat?.mimeType || '';
  const vExt = vMime.includes('webm') ? 'webm' : 'mp4';
  const aExt = aMime.includes('webm') ? 'webm' : 'm4a';
  videoPath = join(outdir, `video.${vExt}`);
  audioPath = join(outdir, `audio.${aExt}`);
  console.error(`[sabr] 产物: video.${vExt} (${vMime}) + audio.${aExt} (${aMime})`);

  // ⚠️ **同步写**（不是 createWriteStream）—— Bun 的写流会把数据缓存在用户态，
  // 文件大小要到 `end()` 才更新，于是进度轮询 8 秒都读到 0、最后一下跳到完成
  // （用户报「SABR 下载好像没用进度，直接 0 到 100」）。同步写每块立即反映到
  // 文件 size，进度才是真的；写盘比网络快，阻塞可接受。
  const vFd = openSync(videoPath, 'w');
  const aFd = openSync(audioPath, 'w');

  const writeAll = (fd, buf) => {
    let off = 0;
    while (off < buf.length) {
      off += writeSync(fd, buf, off, buf.length - off);
    }
  };

  const pump = async (s, fd, add) => {
    const r = s.getReader();
    let first = true;
    for (;;) {
      let chunk;
      try {
        chunk = await r.read();
      } catch (e) {
        // 直播到点我们 abort() 时，库用 controller.error() 关闭流 —— read() 会 reject
        // "Stream aborted"。这是**预期收尾**，不是下载失败；已写盘的字节完整保留。
        if (liveStopped) break;
        throw e;
      }
      const { done, value } = chunk;
      if (done) break;
      if (first) {
        first = false;
        console.error(`[sabr] 收到首个数据块: ${value.length} 字节`);
      }
      add(value.length);
      writeAll(fd, value);
    }
  };

  // ★ 直播怎么停 —— **墙钟 + grace，然后 abort()**。
  //
  // 三个候选判据实测下来只有墙钟可用:
  //   - ``durationMs``（点播的停止条件）对直播无效: 库比较的是 ``playerTimeMs``，直播从
  //     **直播边缘**开始（实测 headTimeMs ≈ 5.8e6 且持续增长），比值永远不成立；
  //   - ``videoEndTimeMs`` 由 ``endTimeTicks/endTimescale`` 算，直播这两个字段是空的
  //     → **恒为 0**（下载了 20MB 仍是 0）；
  //   - ``liveMetadataUpdate.headTimeMs`` 是**批量跳变**的（实测 3 秒墙钟跳了 10 秒），
  //     拿它计时会提前停，抓到的内容少一半。
  //
  // 直播是实时的: 墙钟 N 秒 ≈ 内容 N 秒。多留 ``liveGraceSeconds``（默认 6s）让服务端
  // 的 readahead 把段补齐；abort() 之后**已写盘的字节保留**（实测 16s → 9.1MB 视频
  // + 348KB 音频）。abort 太早才会是 0 字节 —— 那时数据还在库的缓冲区里没 enqueue。
  let liveWatcher = null;
  if (isLive && liveSeconds > 0) {
    const budgetMs = (liveSeconds + Number(input.liveGraceSeconds ?? 6)) * 1000;
    const t0 = Date.now();
    liveWatcher = setInterval(() => {
      const elapsed = Date.now() - t0;
      if (elapsed >= budgetMs) {
        console.error(`[sabr] 直播: ${(elapsed / 1000).toFixed(1)}s 到点 (v=${vb} a=${ab}) → 停止`);
        liveStopped = true;
        clearInterval(liveWatcher);
        liveWatcher = null;
        Promise.resolve(stream.abort()).catch(() => {});
      }
    }, 500);
  }

  try {
    await Promise.all([
      pump(videoStream, vFd, (n) => {
        vb += n;
      }),
      pump(audioStream, aFd, (n) => {
        ab += n;
      }),
    ]);
  } finally {
    if (liveWatcher) clearInterval(liveWatcher);
    // 关 fd 前内核已把数据交给文件系统（write 是同步的），close 只为释放句柄
    try {
      closeSync(vFd);
    } catch {
      /* ignore */
    }
    try {
      closeSync(aFd);
    } catch {
      /* ignore */
    }
  }

  // 落盘字节数必须与流内累计一致 —— 不一致说明写丢了（宁可失败也不要截断的产物）
  const vWritten = statSync(videoPath).size;
  const aWritten = statSync(audioPath).size;
  if (vWritten !== vb || aWritten !== ab) {
    fail(`落盘字节与流累计不一致 (video ${vWritten}/${vb}, audio ${aWritten}/${ab}) —— 写入丢数据`);
  }

  if (vb === 0) fail(streamError || 'no video bytes downloaded');
  if (ab === 0) fail(streamError || 'no audio bytes downloaded');

  console.log(JSON.stringify({
    ok: true,
    videoPath,
    audioPath,
    videoItag: selectedFormats.videoFormat?.itag,
    audioItag: selectedFormats.audioFormat?.itag,
    videoBytes: vb,
    audioBytes: ab,
  }));
  exitAfterFlush(0);
} catch (e) {
  fail(e?.message || e);
}

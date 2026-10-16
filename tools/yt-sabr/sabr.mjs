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
import { createWriteStream, readFileSync, mkdirSync, statSync } from 'node:fs';
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
 * 值形如 ``['av1','vp9','avc']`` —— 按顺序挑第一个在素材里存在的编码。
 * 库自带的 ``chooseFormat`` 只有 preferH264/preferWebM/preferMP4 三个布尔偏好、
 * 且同分辨率下**按数组顺序**取（等于 AVC 优先），拿不到「AV1 > VP9 > AVC」这种次序。
 *
 * 选中的编码同时决定音频：av1/avc → mp4 容器（配 aac itag=140）；
 * vp9 → webm 容器（配 opus itag=251）—— 容器要一致，跨容器 mux 兼容性差。
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
  for (const codec of wanted) {
    const pattern = CODEC_PATTERNS[codec];
    if (!pattern) continue;
    // 1080p 上限内最高的那一档（同编码同分辨率可能有多个 itag，取码率最高）
    const candidates = formats
      .filter((f) => f.mimeType?.startsWith('video/') && f.mimeType.includes(pattern) && (f.height || 0) <= maxHeight)
      .sort((a, b) => (b.height || 0) - (a.height || 0) || (b.bitrate || 0) - (a.bitrate || 0));
    if (candidates.length) {
      const video = candidates[0];
      const spec = CODEC_AUDIO[codec];
      const acodec = spec?.audio || 'mp4a';
      // ⚠️ 音频要挑**默认音轨**：同一 itag 有 xtags=undefined / 'CggKA2RyYxIBMQ'(dubbed) /
      // 'CgcKAnZiEgEx'(secondary) 三个变体，按 bitrate 排会选到非默认音轨（实测踩过：
      // av1 因此整条被跳过）。默认音轨 = xtags 为空。
      const audios = formats
        .filter((f) => f.mimeType?.startsWith('audio/') && f.mimeType.includes(acodec))
        .sort((a, b) => {
          const aDefault = a.xtags ? 1 : 0;
          const bDefault = b.xtags ? 1 : 0;
          return aDefault - bDefault || (b.bitrate || 0) - (a.bitrate || 0);
        });
      if (audios.length) return { video, audio: audios[0], codec };
    }
  }
  return null;
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

let vb = 0, ab = 0;

try {
  const stream = new SabrStream({
    fetch: fetchWithCookie,
    serverAbrStreamingUrl,
    videoPlaybackUstreamerConfig,
    formats,
    clientInfo: { clientName: 1, clientVersion: '2.20261015.01.00' },
  });

  let streamError = null;
  stream.on('error', (e) => {
    streamError = e?.message || String(e);
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

  const { videoStream, audioStream, selectedFormats } = await stream.start({
    videoQuality: input.videoQuality || '1080p',
    enabledTrackTypes: EnabledTrackTypes.VIDEO_AND_AUDIO,
    maxRetries: 3,
    stallDetectionMs: 30000,
    // 显式格式（itag 或对象都行）优先；都没有才让库按容器偏好选
    videoFormat: videoSpec,
    audioFormat: audioSpec,
    preferMP4: !videoSpec && input.preferMp4 !== false,
    preferWebM: !videoSpec && !!input.preferWebM,
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

  const vOut = createWriteStream(videoPath);
  const aOut = createWriteStream(audioPath);

  const pump = async (s, out, add) => {
    const r = s.getReader();
    for (;;) {
      const { done, value } = await r.read();
      if (done) break;
      add(value.length);
      // ⚠️ **必须等 write 回调** —— 只调 out.write() 不等的话, 流还在缓冲区里就
      // 走到 process.exit(0), 磁盘慢时静默丢数据（实测同一视频落盘 27MB/44MB 不定）。
      await new Promise((resolve, reject) => {
        out.write(value, (err) => (err ? reject(err) : resolve()));
      });
    }
  };

  await Promise.all([
    pump(videoStream, vOut, (n) => { vb += n; }),
    pump(audioStream, aOut, (n) => { ab += n; }),
  ]);

  // 显式关流并等 flush（end 回调 = 数据真正落盘）
  await Promise.all([
    new Promise((resolve) => vOut.end(resolve)),
    new Promise((resolve) => aOut.end(resolve)),
  ]);

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

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
import { createWriteStream, readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';

const args = {};
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i].replace(/^--/, '');
  args[key] = process.argv[i + 1];
}

const inputPath = args.input;
const outdir = args.outdir || '/tmp/yt-sabr-out';
if (!inputPath) fail('missing --input');

function fail(msg) {
  console.log(JSON.stringify({ ok: false, error: String(msg) }));
  process.exit(1);
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
const videoPath = join(outdir, 'video.mp4');
const audioPath = join(outdir, 'audio.m4a');

const formats = adaptiveFormats.map(buildSabrFormat);
console.error(`[sabr] formats=${formats.length} quality=${input.videoQuality || '1080p'}`);

// 自定义 fetch: 只补 cookie（SabrStream 自己管 headers/body）
const fetchWithCookie = (url, options = {}) => {
  const headers = new Headers(options.headers || {});
  if (cookie) headers.set('cookie', cookie);
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

  const { videoStream, audioStream, selectedFormats } = await stream.start({
    videoQuality: input.videoQuality || '1080p',
    enabledTrackTypes: EnabledTrackTypes.VIDEO_AND_AUDIO,
    maxRetries: 3,
    stallDetectionMs: 30000,
    // 优先 mp4 容器（h264 + aac），与输出 mp4 最兼容；拿不到就退回默认
    preferMP4: input.preferMp4 !== false,
  });

  console.error(`[sabr] selected video=${selectedFormats.videoFormat?.itag} audio=${selectedFormats.audioFormat?.itag}`);

  const vOut = createWriteStream(videoPath);
  const aOut = createWriteStream(audioPath);

  const pump = async (s, out, add) => {
    const r = s.getReader();
    for (;;) {
      const { done, value } = await r.read();
      if (done) break;
      add(value.length);
      out.write(value);
    }
  };

  await Promise.all([
    pump(videoStream, vOut, (n) => { vb += n; }),
    pump(audioStream, aOut, (n) => { ab += n; }),
  ]);

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
  process.exit(0);
} catch (e) {
  fail(e?.message || e);
}

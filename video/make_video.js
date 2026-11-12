#!/usr/bin/env node
/*
 * Halyava VPN — ежедневный рекламный ролик.
 * Берёт свежие цифры из vpn/sub/stats.json, выбирает тему дня и делает:
 *   video/out/halyava-ГГГГ-ММ-ДД.mp4  — вертикальное видео 1080×1920, 15 с (Reels, Shorts, TikTok, Клипы VK)
 *   video/out/halyava-ГГГГ-ММ-ДД.txt  — описание и хэштеги к нему
 * Запуск: node video/make_video.js
 * Нужны: playwright (Chromium) и ffmpeg.
 */
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const OUT = path.join(__dirname, 'out');
const FPS = 30, BASE = 15;   // сцены рассчитаны на 15 с; под длинную озвучку ролик растягивается
const SITE = 'timofeikislakov5-collab.github.io/halyava-vpn';
const SUB = 'https://raw.githubusercontent.com/timofeikislakov5-collab/halyava-vpn/main/vpn/sub/russia.txt';

const stats = JSON.parse(fs.readFileSync(path.join(ROOT, 'vpn/sub/stats.json'), 'utf8'));
const now = new Date(Date.now() + 3 * 3600e3);              // московское время
const date = now.toISOString().slice(0, 10);
const dayN = Math.floor(now.getTime() / 86400e3);
const working = stats.working ?? stats.russia;
const time = (stats.updated || '').split(' ')[1] || '';

// Темы роликов — меняются каждый день по кругу
const THEMES = [
  { hook: 'VPN за <em>0 ₽</em>', sub: 'Без подписки. Без регистрации.', a1: '#4d7bff', a2: '#30e0c8', word: 'Навсегда.',
    title: 'VPN за 0 ₽ — серьёзно 🔥', text: 'Сотни серверов в одной ссылке. Без подписки, без регистрации, без карты.' },
  { hook: 'Хватит <em>платить</em> за VPN', sub: 'Есть бесплатный — одной ссылкой', a1: '#ff5e7e', a2: '#ffb347', word: 'Без карты.',
    title: 'Хватит платить за VPN 💸', text: 'Добавляешь одну ссылку в Happ — и у тебя сотни бесплатных серверов, лучшие всегда сверху.' },
  { hook: 'Белые списки? <em>Не беда</em>', sub: 'Серверы для мобильного интернета', a1: '#00c2a8', a2: '#7cf29a', word: 'Работает.',
    title: 'Мобильный интернет урезали? 📱', text: 'В подписке есть отдельная группа серверов Whitelist — для мобильного интернета с белыми списками.' },
  { hook: `<em>${working}</em> серверов работают`, sub: `Проверены сегодня${time ? ' в ' + time : ''}`, a1: '#7b61ff', a2: '#3ee0ff', word: 'Каждые 3 часа.',
    title: `${working} рабочих серверов сегодня ✅`, text: 'Каждые 3 часа робот проверяет все серверы и ставит самые быстрые наверх.' },
  { hook: 'Твой VPN опять <em>отвалился?</em>', sub: 'Этот обновляется каждые 3 часа', a1: '#ff7a3d', a2: '#ffd36a', word: 'Само.',
    title: 'VPN, который чинит себя сам 🔄', text: 'Мёртвые серверы заменяются новыми каждые 3 часа. Просто нажми «обновить» в приложении.' },
  { hook: 'Один клик — <em>и готово</em>', sub: 'Добавь ссылку в Happ', a1: '#3d8bff', a2: '#9b7bff', word: 'За минуту.',
    title: 'Подключение за 1 минуту ⚡', text: 'Скопируй ссылку → вставь в Happ → жми «Подключить». Всё.' },
  { hook: '<em>ТОП</em> серверов дня', sub: 'Самые быстрые — всегда первыми', a1: '#ffb020', a2: '#ff5e7e', word: 'Сегодня.',
    title: 'ТОП серверов дня 🏆', text: 'В подписке значки: 🔥 TOP, ⚡ быстрый, ⭐ стабильный. Выбирай лучший сразу.' },
];
const th = THEMES[dayN % THEMES.length];

// ---------- озвучка ----------
function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  return b === 1 ? one : b >= 2 && b <= 4 ? few : many;
}
const T = stats.russia, W = working;
const srv = n => `${n} ${plural(n, 'сервер', 'сервера', 'серверов')}`;
const work = n => `${n} ${plural(n, 'работает', 'работают', 'работают')}`;
// Озвучка из 5 фраз — по одной на сцену: хук · цифры · ТОП · 3 шага · призыв.
// Между фразами пауза: по паузам ролик подстраивает смену сцен под голос.
const WRK = `${srv(W)} ${plural(W, 'работает', 'работают', 'работают')}`;
const VOICE = [
  ['Ви-пи-эн за ноль рублей.', `${srv(T)} в одной ссылке. ${WRK} прямо сейчас.`, 'Самые быстрые — всегда сверху. Обновление каждые три часа.', 'Скопируй ссылку. Вставь в Хэпп. Подключайся.', 'Халява ви-пи-эн. Бесплатно. Ссылка в профиле.'],
  ['Хватит платить за ви-пи-эн.', `Вот ${srv(T)} — бесплатно и в одной ссылке.`, 'Лучшие серверы всегда наверху списка.', 'Скопируй ссылку. Вставь в Хэпп. Подключайся.', 'Без подписки и без карты. Ссылка в профиле.'],
  ['Белые списки на мобильном? Не проблема.', `${srv(T)}, включая отдельные серверы для белых списков.`, 'Самые быстрые — всегда первыми.', 'Скопируй ссылку. Вставь в Хэпп. Подключайся.', 'Халява ви-пи-эн. Ссылка в профиле.'],
  [`${WRK} прямо сейчас.`, `Всего ${srv(T)} в одной ссылке.`, 'Каждые три часа — проверка, и быстрые встают наверх.', 'Скопируй ссылку. Вставь в Хэпп. Подключайся.', 'Бесплатно. Ссылка в профиле.'],
  ['Ви-пи-эн опять отвалился?', `Здесь ${srv(T)}, и список обновляется сам.`, 'Каждые три часа мёртвые серверы меняются на живые.', 'Скопируй ссылку. Вставь в Хэпп. Подключайся.', 'Халява ви-пи-эн. Ссылка в профиле.'],
  ['Подключение за одну минуту.', `${srv(T)} — в одной ссылке.`, 'Лучшие отмечены огнём и стоят первыми.', 'Скопируй ссылку. Вставь в Хэпп. Подключайся.', 'Бесплатно. Ссылка в профиле.'],
  ['Топ серверов дня.', `${WRK} прямо сейчас.`, 'Самые быстрые — всегда первыми.', 'Скопируй ссылку. Вставь в Хэпп. Подключайся.', 'Халява ви-пи-эн. Ссылка в профиле.'],
];
const voiceParts = VOICE[dayN % VOICE.length];
const voiceText = voiceParts.join('\n\n');

const { execFileSync } = require('child_process');
function duration(file) {
  return parseFloat(execFileSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', file]).toString()) || 0;
}
// Сначала нейроголос Microsoft (edge-tts), если недоступен — RHVoice, если и его нет — без голоса.
// Движки озвучки по порядку: ElevenLabs (если есть ключ) → нейроголос Microsoft → RHVoice.
const GAP = 0.45;                                     // пауза между фразами, с
const ENGINES = [
  ['ElevenLabs', () => !!process.env.ELEVENLABS_API_KEY, (text, out) => execFileSync('curl', ['-sS', '--fail', '--max-time', '90', '-o', out, '-X', 'POST',
      `https://api.elevenlabs.io/v1/text-to-speech/${process.env.ELEVENLABS_VOICE || 'SkGOoi8yfK3ONMNKMKj0'}?output_format=mp3_44100_128`,   // Maxim — уверенный баритон
      '-H', 'xi-api-key: ' + process.env.ELEVENLABS_API_KEY, '-H', 'Content-Type: application/json',
      '-d', JSON.stringify({ text, model_id: process.env.ELEVENLABS_MODEL || 'eleven_multilingual_v2',
        voice_settings: { stability: 0.55, similarity_boost: 0.8, style: 0.35, use_speaker_boost: true } })],
      { stdio: 'pipe', timeout: 100000 }), '.mp3'],
  ['нейроголос Microsoft', () => true, (text, out) => execFileSync('edge-tts', ['--voice', process.env.TTS_VOICE || 'ru-RU-DmitryNeural',
      '--rate=+4%', '--pitch=-4Hz', '--text', text, '--write-media', out], { stdio: 'pipe', timeout: 60000 }), '.mp3'],
  ['RHVoice', () => true, (text, out) => execFileSync('RHVoice-test', ['-p', process.env.RHVOICE_VOICE || 'aleksandr-hq', '-r', '105', '-o', out],
      { input: text, stdio: ['pipe', 'pipe', 'pipe'], timeout: 60000 }), '.wav'],
];

// Озвучивает каждую фразу отдельно и склеивает с паузами — так точно известно, где начинается каждая фраза.
// Возвращает { file, starts } или null.
function makeVoice(base) {
  if (process.env.VOICE_FILE && fs.existsSync(process.env.VOICE_FILE)) {   // готовая озвучка одним файлом
    console.log('Озвучка: готовый файл', process.env.VOICE_FILE);
    return { file: process.env.VOICE_FILE, starts: null };
  }
  for (const [name, ok, synth, ext] of ENGINES) {
    if (!ok()) continue;
    try {
      const files = voiceParts.map((text, i) => { const f = `${base}-${i}${ext}`; synth(text, f); if (duration(f) < 0.3) throw new Error('пустой файл'); return f; });
      const durs = files.map(duration);
      const out = base + '.wav';
      const inputs = files.flatMap(f => ['-i', f]);
      const chain = files.map((_, i) => `[${i}:a]aresample=44100,aformat=channel_layouts=mono${i < files.length - 1 ? `,apad=pad_dur=${GAP}` : ''}[p${i}]`).join(';')
        + ';' + files.map((_, i) => `[p${i}]`).join('') + `concat=n=${files.length}:v=0:a=1[a]`;
      execFileSync('ffmpeg', ['-y', '-loglevel', 'error', ...inputs, '-filter_complex', chain, '-map', '[a]', out]);
      const starts = []; let acc = 0;
      durs.forEach(d => { starts.push(acc); acc += d + GAP; });
      console.log('Озвучка:', name);
      return { file: out, starts };
    } catch (e) { console.log(name, 'недоступен:', String(e.message).split('\n')[0]); }
  }
  console.log('Озвучки нет — ролик будет без голоса');
  return null;
}

const top = (stats.top || []).map(t => ({ cc: t.cc, tag: t.tag || (t.name || '').split(' · ')[1], type: t.type || t.proto, ms: t.ms, stable: t.stable }));

const AD = {
  hook: th.hook, sub: th.sub, a1: th.a1, a2: th.a2, ctaWord: th.word,
  total: stats.russia, working, updated: time, url: SITE, cta: 'Ссылка в профиле 👆', top,
};

const TAGS = ['#vpn', '#впн', '#бесплатныйвпн', '#halyavavpn', '#happ', '#vless', '#v2ray', '#интернет',
  '#лайфхак', '#полезное', '#смартфон', '#айфон', '#андроид', '#рекомендации', '#fyp'];

function caption() {
  const extra = TAGS.slice(6); const tags = [...TAGS.slice(0, 6), ...extra.map((_, k) => extra[(k + dayN) % extra.length]).slice(0, 6)];
  return [
    `${th.title}`,
    ``,
    `${th.text}`,
    ``,
    `✅ Серверов: ${stats.russia}, работают прямо сейчас: ${working}`,
    `📶 Wi-Fi · 📱 Мобильный · 🛡 Белые списки`,
    `🔄 Обновляется каждые 3 часа`,
    `💸 Бесплатно, без регистрации`,
    ``,
    `Как подключить:`,
    `1️⃣ Установи Happ (App Store / Google Play)`,
    `2️⃣ Открой сайт и нажми «Добавить в Happ»`,
    `3️⃣ Выбери сервер с 🔥 и жми «Подключить»`,
    ``,
    `🌐 ${SITE}`,
    ``,
    tags.join(' '),
    ``,
    `— — —`,
    `Для Telegram / VK (там ссылки кликабельны):`,
    `Сайт: https://${SITE}/`,
    `Подписка: ${SUB}`,
    ``,
    `Заголовок для YouTube Shorts: ${th.title.replace(/[^\p{L}\p{N}\s₽!?.,—-]/gu, '').trim()} | Halyava VPN #shorts`,
  ].join('\n');
}

// ---------- синхронизация сцен с голосом ----------
const SCENES = [0, 2.8, 5.8, 10, 12.8, BASE];       // начало сцен в ad.html (в секундах из 15)
const DELAY = 0.35;                                  // голос стартует через 0,35 с
// Находит 4 самые длинные паузы в озвучке — это границы между 5 фразами.
function sceneCuts(file, dur) {
  let out = '';
  try {
    out = require('child_process').spawnSync('ffmpeg', ['-hide_banner', '-i', file, '-af', 'silencedetect=noise=-32dB:d=0.18', '-f', 'null', '-'],
      { encoding: 'utf8' }).stderr;
  } catch (e) { return null; }
  const starts = [...out.matchAll(/silence_start: ([\d.]+)/g)].map(m => +m[1]);
  const ends = [...out.matchAll(/silence_end: ([\d.]+) \| silence_duration: ([\d.]+)/g)].map(m => ({ end: +m[1], len: +m[2] }));
  const total = duration(file);
  const gaps = ends.filter(g => g.end < total - 0.3 && g.end - g.len > 0.3)          // не в начале и не в конце
    .sort((a, b) => b.len - a.len).slice(0, 4).sort((a, b) => a.end - b.end);
  if (gaps.length < 4) return null;
  const cuts = [0, ...gaps.map(g => g.end + DELAY - 0.12), dur];                     // сцена появляется чуть до фразы
  for (let i = 1; i < cuts.length; i++) if (cuts[i] - cuts[i - 1] < 1.2) return null;
  return cuts;
}
// Переводит реальное время ролика во время анимации: каждая сцена растягивается под свою фразу.
function mapTime(t, cuts, dur) {
  if (!cuts) return t * BASE / dur;
  for (let i = 0; i < 5; i++) {
    if (t <= cuts[i + 1] || i === 4) {
      const k = (t - cuts[i]) / (cuts[i + 1] - cuts[i]);
      return SCENES[i] + Math.min(1, Math.max(0, k)) * (SCENES[i + 1] - SCENES[i]);
    }
  }
  return BASE;
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const mp4 = path.join(OUT, `halyava-${date}.mp4`);
  const txt = path.join(OUT, `halyava-${date}.txt`);
  fs.writeFileSync(txt, caption() + '\n\n— — —\nТекст озвучки:\n' + voiceText + '\n');

  const voice = makeVoice(path.join(OUT, `voice-${date}`));
  const DUR = voice ? Math.max(BASE, Math.ceil((duration(voice.file) + 1.3) * 10) / 10) : BASE;
  const cuts = !voice ? null : voice.starts ? [0, ...voice.starts.slice(1).map(x => x + DELAY - 0.12), DUR] : sceneCuts(voice.file, DUR);
  console.log('Смена сцен под голос:', cuts ? cuts.map(x => x.toFixed(2)).join(' · ') : 'равномерно');
  console.log('Длина ролика:', DUR, 'с');

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1080, height: 1920 }, deviceScaleFactor: 1 });
  await page.addInitScript(ad => { window.AD = ad; }, AD);
  await page.goto('file://' + path.join(__dirname, 'ad.html'), { waitUntil: 'networkidle' }).catch(() => {});
  await page.evaluate(() => document.fonts && document.fonts.ready);

  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error',
    '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-',
    ...(voice ? ['-i', voice.file] : ['-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100']),
    ...(voice ? ['-filter_complex', '[1:a]adelay=350|350,loudnorm=I=-14:TP=-1.5,apad[a]', '-map', '0:v', '-map', '[a]'] : []),
    '-t', String(DUR), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'medium', '-crf', '20',
    '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', mp4], { stdio: ['pipe', 'inherit', 'inherit'] });

  const frames = FPS * DUR;
  for (let f = 0; f < frames; f++) {
    await page.evaluate(t => window.render(t), mapTime(f / FPS, cuts, DUR));
    const buf = await page.screenshot({ type: 'jpeg', quality: 92 });
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
    if (f % 90 === 0) process.stdout.write(`кадр ${f}/${frames}\n`);
  }
  ff.stdin.end();
  await new Promise((res, rej) => ff.on('close', c => c === 0 ? res() : rej(new Error('ffmpeg ' + c))));
  await browser.close();
  console.log('Готово:', mp4, '\nОписание:', txt);
})().catch(e => { console.error(e); process.exit(1); });

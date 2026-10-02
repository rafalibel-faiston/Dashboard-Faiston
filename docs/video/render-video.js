// Exporta static/video.html para MP4 (1920x1080, 30 fps), quadro a quadro,
// com a trilha de efeitos sonoros que a própria página sintetiza.
// Uso: npm i -g playwright && node docs/video/render-video.js
// Requer ffmpeg no PATH. Saída: docs/video/faiston-ops.mp4
const { chromium } = require('playwright');
const { execSync } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path');

(async () => {
    const root = path.resolve(__dirname, '../..');
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'faiston-video-'));
    const browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
    await page.goto('file://' + path.join(root, 'static/video.html') + '?capture=1');
    await page.evaluate(() => document.fonts.ready);
    const FPS = 30;
    const total = Math.round(await page.evaluate(() => window.__DURATION) * FPS);
    for (let i = 0; i < total; i++) {
        await page.evaluate(t => window.__seek(t), i / FPS);
        await page.screenshot({ path: path.join(tmp, `f${String(i).padStart(4, '0')}.jpg`), type: 'jpeg', quality: 95 });
    }
    const wav = await page.evaluate(() => window.__audioWav());
    await browser.close();
    const out = path.join(__dirname, 'faiston-ops.mp4');
    let audioIn = '', audioOut = '';
    if (wav) {
        fs.writeFileSync(path.join(tmp, 'track.wav'), Buffer.from(wav, 'base64'));
        audioIn = `-i "${tmp}/track.wav"`;
        audioOut = '-c:a aac -b:a 192k -shortest';
    }
    execSync(`ffmpeg -y -loglevel error -framerate ${FPS} -i "${tmp}/f%04d.jpg" ${audioIn} -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p ${audioOut} -movflags +faststart "${out}"`, { stdio: 'inherit' });
    fs.rmSync(tmp, { recursive: true, force: true });
    console.log(`${total} quadros -> ${out}`);
})();

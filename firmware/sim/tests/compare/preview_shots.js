// Puts preview/bento.html into each scene of scenes.json and saves both boards' canvases. Run by compare.py.
//   NODE_PATH=<folder with playwright-core> node preview_shots.js <scenes.json> <out dir>
const { chromium } = require("playwright-core");
const fs = require("fs");
const path = require("path");

const PAGE = "file://" + path.resolve(__dirname, "../../../../preview/bento.html");
const scenes = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = process.argv[3];
fs.mkdirSync(out, { recursive: true });

(async () => {
  // Google Chrome, where macOS installs it, or wherever CHROME says.
  const chrome = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const browser = await chromium.launch({ executablePath: chrome, headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: "light" })).newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  for (const scene of scenes) {
    await page.goto(PAGE);
    await page.waitForTimeout(350);
    // Times and animations held still where the scene asks: the same moment as the firmware's #anim.
    await page.evaluate(() => {
      window.__b = window.__bento;
      window.__S = window.__bento.S;
      const realNow = performance.now.bind(performance);
      window.__t0 = realNow();
    });
    await page.evaluate(scene.js);
    await page.waitForTimeout(scene.wait || 450);
    for (const id of ["knob", "square"]) {
      // The pulse: wait for the top of the heartbeat, just after the page's own frame has drawn it.
      const grab = scene.peak
        ? (i) => new Promise((done) => {
            const f = (t) => { const ph = (t % 1000) / 1000; if (ph > 0.15 && ph < 0.21) done(document.getElementById(`cv-${i}`).toDataURL("image/png")); else requestAnimationFrame(f); };
            requestAnimationFrame(f);
          })
        : (i) => document.getElementById(`cv-${i}`).toDataURL("image/png");
      const data = await page.evaluate(grab, id);
      fs.writeFileSync(path.join(out, `${scene.name}-${id}.png`), Buffer.from(data.split(",")[1], "base64"));
    }
  }
  await browser.close();
  if (errors.length) { console.error(errors.join("\n")); process.exit(1); }
  console.log(`preview: ${scenes.length} scenes`);
})();

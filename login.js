const INJECTED_SCRIPT = `
(function() {
    if (window.self === window.top) return;

    const fixedScreenX = 800 + Math.floor(Math.random() * 400);
    const fixedScreenY = 400 + Math.floor(Math.random() * 200);
    try {
        Object.defineProperty(MouseEvent.prototype, 'screenX', { get: () => fixedScreenX });
        Object.defineProperty(MouseEvent.prototype, 'screenY', { get: () => fixedScreenY });
    } catch(e) {}

    window.__turnstile_state = 'idle';
    window.__turnstile_data  = null;

    const reportedRoots = new WeakSet();

    function attachCheckboxWatcher(shadowRoot, checkbox) {
        checkbox.addEventListener('change', () => {
            if (checkbox.checked) {
                window.__turnstile_state = 'solved';
                window.__turnstile_data  = null;
            }
        });
        const mo = new MutationObserver(() => {
            if (checkbox.checked) {
                window.__turnstile_state = 'solved';
                window.__turnstile_data  = null;
                mo.disconnect();
            }
        });
        mo.observe(checkbox, { attributes: true, attributeFilter: ['checked'] });
    }

    function checkShadowRoot(shadowRoot) {
        if (reportedRoots.has(shadowRoot)) return false;
        const checkbox = shadowRoot.querySelector('input[type="checkbox"]');
        if (checkbox) {
            const rect = checkbox.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                if (checkbox.checked) {
                    window.__turnstile_state = 'solved';
                    return false;
                }
                window.__turnstile_data = {
                    clientX: rect.left + rect.width  / 2,
                    clientY: rect.top  + rect.height / 2,
                };
                window.__turnstile_state = 'found';
                reportedRoots.add(shadowRoot);
                attachCheckboxWatcher(shadowRoot, checkbox);
                return true;
            }
        }
        return false;
    }

    const originalAttachShadow = Element.prototype.attachShadow;
    Element.prototype.attachShadow = function(init) {
        const openInit = Object.assign({}, init, { mode: 'open' });
        const shadowRoot = originalAttachShadow.call(this, openInit);
        if (shadowRoot) {
            const observer = new MutationObserver(() => {
                if (checkShadowRoot(shadowRoot)) observer.disconnect();
            });
            observer.observe(shadowRoot, { childList: true, subtree: true });
        }
        return shadowRoot;
    };

    function scanAll() {
        if (window.__turnstile_state === 'solved') return;
        document.querySelectorAll('*').forEach(el => {
            if (el.shadowRoot) checkShadowRoot(el.shadowRoot);
        });
    }
    const scanInterval = setInterval(() => {
        scanAll();
        if (window.__turnstile_state === 'solved') clearInterval(scanInterval);
    }, 400);
    scanAll();
})();
`;

async function humanLikeClick(client, x, y) {
    const startX = x + (Math.random() - 0.5) * 60;
    const startY = y + (Math.random() - 0.5) * 60;
    const steps  = 8 + Math.floor(Math.random() * 5);

    for (let i = 0; i <= steps; i++) {
        const p = i / steps;
        const e = p < 0.5 ? 2 * p * p : -1 + (4 - 2 * p) * p; 
        await client.send('Input.dispatchMouseEvent', {
            type: 'mouseMoved',
            x: startX + (x - startX) * e,
            y: startY + (y - startY) * e,
        });
        await new Promise(r => setTimeout(r, 8 + Math.random() * 12));
    }

    await client.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await new Promise(r => setTimeout(r, 80 + Math.random() * 120)); 
    await client.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
}

async function attemptTurnstileCdp(page) {
    const frames = page.frames();
    for (const frame of frames) {
        try {
            const info = await frame.evaluate(() => ({ state: window.__turnstile_state, data:  window.__turnstile_data })).catch(() => null);
            if (!info || !info.data) continue;
            if (info.state === 'solved' || info.state === 'clicked') return false;

            console.log('🛡️ 发现 Cloudflare Turnstile，执行仿人类 CDP 点击...');
            const iframeElement = await frame.frameElement();
            if (!iframeElement) continue;
            const box = await iframeElement.boundingBox();
            if (!box) continue;

            const clickX = box.x + info.data.clientX;
            const clickY = box.y + info.data.clientY;

            const client = await page.context().newCDPSession(page);
            await humanLikeClick(client, clickX, clickY);
            await client.detach();

            await frame.evaluate(() => { window.__turnstile_state = 'clicked'; window.__turnstile_data  = null; }).catch(() => {});
            return true;
        } catch (e) {}
    }
    return false;
}

const LOGIN_URL = 'https://dash.hidencloud.com/auth/login';
const DASH_URL = 'https://dash.hidencloud.com/dashboard';
const EMAIL_SEL = 'input[name="username"], input#username, input[name="email"], input[type="email"], input[name="EMAIL"]';
const PWD_SEL = 'input[name="password"], input#password, input[name="PASSWORD"], input[type="password"]';
const SUBMIT_SEL = 'button[type="submit"], button:has-text("Sign in"), button:has-text("Login"), button:has-text("登录")';

// ================= Turnstile 处理（移植自 Auto-Renew-HidenCloud/app.py）=================
// 新版 Turnstile 的挑战 iframe 可能在闭包 shadow DOM 里，querySelector 找不到，
// 但 page.frames() 能看到，frameElement() 能拿到元素；隐藏 token 输入框一定在 light DOM，可作兜底定位。
// 点击：首选 frameElement.click(position)，失败再用 CDP 底层鼠标事件兜底。
const TS_MARKER = 'challenges.cloudflare.com';
const TS_IFRAME_SEL = 'iframe[src*="challenges.cloudflare.com"], iframe[title*="Cloudflare"]';
const cdpSessions = new WeakMap();

function overlaps(box, boxes, dx = 25, dy = 25, dw = 60) {
    return boxes.some(b => Math.abs(b.x - box.x) < dx && Math.abs(b.y - box.y) < dy && Math.abs(b.width - box.width) < dw);
}

async function tsState(page) {
    try {
        return await page.evaluate(() => {
            let total = 0, solved = 0;
            document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').forEach(n => {
                total++;
                if (n.value && n.value.length > 20) solved++;
            });
            return { total, solved };
        });
    } catch (e) { return { total: 0, solved: 0 }; }
}

async function pageReady(page) {
    try {
        const t = ((await page.title()) || '').toLowerCase();
        const blocked = ['just a moment', 'attention required', 'checking your browser', '请稍候', 'security verification', '请验证'];
        return !!t && !blocked.some(k => t.includes(k));
    } catch (e) { return false; }
}

async function cdpClickAt(page, x, y, log) {
    try {
        let session = cdpSessions.get(page);
        if (!session) {
            session = await page.context().newCDPSession(page);
            cdpSessions.set(page, session);
        }
        const sx = x - (50 + Math.random() * 60);
        const sy = y - (35 + Math.random() * 40);
        const steps = 8 + Math.floor(Math.random() * 7);
        for (let i = 1; i <= steps; i++) {
            const ix = sx + (x - sx) * i / steps + (Math.random() * 3 - 1.5);
            const iy = sy + (y - sy) * i / steps + (Math.random() * 3 - 1.5);
            await session.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: ix, y: iy });
            await page.waitForTimeout(10 + Math.random() * 25);
        }
        await page.waitForTimeout(100 + Math.random() * 150);
        await session.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 });
        await page.waitForTimeout(50 + Math.random() * 70);
        await session.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
        return true;
    } catch (e) {
        log(`⚠️ CDP 底层点击失败: ${e.message.split('\n')[0]}`);
        try { const s = cdpSessions.get(page); if (s) await s.detach(); } catch (e2) {}
        cdpSessions.delete(page);
        return false;
    }
}

async function challengeFrames(page) {
    const targets = [], seen = [];
    try {
        for (const f of page.frames()) {
            if (!(f.url() || '').includes(TS_MARKER)) continue;
            try {
                const fe = await f.frameElement();
                if (await fe.isVisible()) {
                    const box = await fe.boundingBox();
                    if (box && box.width > 10 && box.height > 10) { seen.push(box); targets.push([fe, box]); }
                }
            } catch (e) {}
        }
    } catch (e) {}
    try {
        const els = await page.locator(TS_IFRAME_SEL).all();
        for (const el of els) {
            try {
                if (!(await el.isVisible())) continue;
                const box = await el.boundingBox();
                if (box && box.width > 10 && box.height > 10 && !overlaps(box, seen)) { seen.push(box); targets.push([el, box]); }
            } catch (e) {}
        }
    } catch (e) {}
    return targets;
}

async function challengeContainers(page) {
    const targets = [];
    try {
        const els = await page.locator('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').all();
        for (const el of els) {
            try {
                if (await el.evaluate(n => !!(n.value && n.value.length > 20))) continue;
                const box = await el.evaluate(n => {
                    let p = n.parentElement;
                    for (let i = 0; i < 4 && p; i++) {
                        const r = p.getBoundingClientRect();
                        if (r.width > 40 && r.height > 20) return { x: r.x, y: r.y, width: r.width, height: r.height };
                        p = p.parentElement;
                    }
                    return null;
                });
                if (box && !overlaps(box, targets.map(t => t[1]))) targets.push([null, box]);
            } catch (e) {}
        }
    } catch (e) {}
    return targets;
}

async function challengeBoxes(page) {
    const frames = await challengeFrames(page);
    const seen = frames.map(t => t[1]);
    const targets = frames.slice();
    for (const t of await challengeContainers(page)) if (!overlaps(t[1], seen)) targets.push(t);
    return targets;
}

// 通过信号（任一满足）: successCheck 成立 / 出现新 token 且全部 widget 已解决 / 挑战框被处理后持续消失
// requirePositive=true: 页面没出现 Turnstile 时不会提前返回
// reloadAfter: 累计点击 N 次仍未通过则刷新页面重试（最多 2 次）
async function solveTurnstile(page, { timeout = 120, successCheck = null, requirePositive = false, appearGrace = 5, reloadAfter = 0, shot = 'turnstile_timeout.png', log = console.log } = {}) {
    log('🛡️ 开始处理 Turnstile...');
    const start = Date.now();
    const base = await tsState(page);
    let hadIframe = false, goneSince = null, containerOnlySince = null, clickCount = 0, reloadDone = 0;

    while (Date.now() - start < timeout * 1000) {
        if (successCheck) {
            try { if (await successCheck()) { log('✅ Turnstile 验证通过！'); return true; } } catch (e) {}
        }

        const st = await tsState(page);
        if (st.total > 0 && st.solved >= st.total && (st.total > base.total || st.solved > base.solved)) {
            log(`✅ Turnstile 验证通过（token 已生成 ${st.solved}/${st.total}）！`);
            return true;
        }

        const frames = await challengeFrames(page);
        const seen = frames.map(t => t[1]);
        const targets = frames.concat((await challengeContainers(page)).filter(t => !overlaps(t[1], seen)));

        if (frames.length) {
            hadIframe = true; goneSince = null; containerOnlySince = null;
        } else if (targets.length) {
            hadIframe = true; goneSince = null;
            if (!containerOnlySince) containerOnlySince = Date.now();
            else if (Date.now() - containerOnlySince >= 12000) { log('✅ Turnstile 验证通过（挑战已结束）！'); return true; }
        } else {
            containerOnlySince = null;
            if (hadIframe) {
                if (!goneSince) goneSince = Date.now();
                else if (Date.now() - goneSince >= 8000) { log('✅ Turnstile 验证通过（挑战框已消失）！'); return true; }
            } else if (!requirePositive && !successCheck && Date.now() - start >= appearGrace * 1000) {
                log('ℹ️ 页面未出现 Turnstile，无需处理');
                return true;
            }
            await page.waitForTimeout(1000);
            continue;
        }

        // 逐个点击挑战框：Playwright 定点点击为主，CDP 底层点击兜底
        for (const [el, box] of targets) {
            let clicked = false;
            try {
                const offX = Math.min(30, box.width / 2);
                const posY = box.height / 2;
                // 日志显示：Playwright 点击在外部 CDP 连接的 Chrome 上可能“点了但没生效”，
                // 而 CDP 底层点击生效过，所以奇数次改用 CDP 底层点击，两种方式轮流尝试
                const useCdpOnly = clickCount % 2 === 1;
                if (el && !useCdpOnly) {
                    try { await el.scrollIntoViewIfNeeded({ timeout: 3000 }); } catch (e) {}
                    try {
                        await el.click({ position: { x: offX, y: posY }, timeout: 5000 });
                        clicked = true;
                        log(`🖱️ 点击 Turnstile 验证 (${Math.round(box.x + offX)}, ${Math.round(box.y + posY)}) ...`);
                    } catch (e) {
                        log('⚠️ 挑战框点击失败,尝试底层点击...');
                    }
                }
                if (!clicked) {
                    const cx = box.x + offX + (Math.random() * 4 - 2);
                    const cy = box.y + posY + (Math.random() * 4 - 2);
                    log(`🖱️ CDP 底层点击 Turnstile (${Math.round(cx)}, ${Math.round(cy)}) ...`);
                    clicked = await cdpClickAt(page, cx, cy, log);
                }
            } catch (e) {
                log(`⚠️ 点击挑战框出错: ${e.message.split('\n')[0]}`);
            }
            clickCount++;
            await page.waitForTimeout(4000 + Math.random() * 2000);
        }

        if (reloadAfter && clickCount >= reloadAfter && reloadDone < 2) {
            reloadDone++;
            log(`🔄 累计点击 ${clickCount} 次未通过，刷新页面重试（第 ${reloadDone}/2 次）...`);
            clickCount = 0; hadIframe = false; goneSince = null; containerOnlySince = null;
            await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 }).catch(e => log(`⚠️ 刷新失败: ${e.message.split('\n')[0]}`));
            await page.waitForTimeout(3000 + Math.random() * 2000);
        }
    }

    log(`❌ Turnstile 处理超时（${timeout}s）`);
    try {
        const cf = page.frames().filter(f => (f.url() || '').includes(TS_MARKER)).map(f => f.url().slice(0, 100));
        const st = await tsState(page);
        log(`🔍 超时现场: cf_frames=${cf.length} token=${JSON.stringify(st)} title=${JSON.stringify(await page.title())}`);
        await page.screenshot({ path: shot });
        log(`📸 已保存超时截图: ${shot}`);
    } catch (e) {}
    return false;
}

async function attemptSingleLogin(page, acc) {
    await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });

    // --- 第一道 Turnstile：通过后才会显示账号密码输入框 ---
    const formVisible = () => page.locator('input[type="password"]').first().isVisible().catch(() => false);
    console.log('🛡️ 处理登录页第一道 Turnstile 验证...');
    if (!(await solveTurnstile(page, { timeout: 180, successCheck: formVisible, reloadAfter: 8, shot: 'login_turnstile1_fail.png' }))) {
        throw new Error('第一道 Turnstile 未通过，无法进入登录表单');
    }

    // --- 填写账号密码 ---
    const emailBox = page.locator(EMAIL_SEL).first();
    const passBox  = page.locator(PWD_SEL).first();
    // Turnstile 通过后网关还会做几秒 "Validating security..." 才渲染表单
    await emailBox.waitFor({ state: 'visible', timeout: 60000 });

    console.log('✅ 页面就绪！开始填写凭据...');
    await emailBox.click();
    await page.waitForTimeout(300 + Math.random() * 200);
    await emailBox.fill('');
    await emailBox.type(acc.username, { delay: 40 + Math.random() * 50 });

    await page.waitForTimeout(400 + Math.random() * 300);
    await passBox.click();
    await page.waitForTimeout(200 + Math.random() * 200);
    await passBox.fill('');
    await passBox.type(acc.password, { delay: 40 + Math.random() * 50 });

    // --- 输入完成后等 8 秒，等第二道 Turnstile 出现 ---
    console.log('⏳ 输入完成，等待第二道 Turnstile 加载...');
    await page.waitForTimeout(8000);

    console.log('🛡️ 处理第二道 Turnstile...');
    if (!(await solveTurnstile(page, { timeout: 90, requirePositive: true, shot: 'login_turnstile2_fail.png' }))) {
        console.log('⚠️ 第二道 Turnstile 未确认通过，仍尝试点击登录...');
    }

    // --- 点击登录按钮 ---
    console.log('👆 点击登录按钮...');
    try {
        await page.locator(SUBMIT_SEL).first().click({ timeout: 15000 });
    } catch (e) {
        await page.screenshot({ path: 'login_submit_fail.png' }).catch(() => {});
        throw new Error('点击登录按钮失败');
    }

    // --- 提交后若再出现 Turnstile，边处理边等待跳转 ---
    console.log('⏳ 等待跳转控制台...');
    await page.waitForTimeout(2000);
    const wrongPwd = () => page.getByText('Incorrect password').isVisible().catch(() => false);
    const leftLogin = () => !page.url().includes('auth/login');
    await solveTurnstile(page, {
        timeout: 45,
        successCheck: async () => leftLogin() || (await wrongPwd()),
        shot: 'login_turnstile3_fail.png'
    });
    await page.waitForURL(u => !u.toString().includes('auth/login'), { timeout: 30000 }).catch(() => {});

    if (!leftLogin()) {
        if (await wrongPwd()) throw new Error('账号密码错误');
        if (await page.getByText('cf-turnstile-response field is required').isVisible().catch(() => false)) throw new Error('CF 验证失效 (Token 被拒绝)');
    }

    // --- 进入 dashboard 确认登录态 ---
    await page.goto(DASH_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await solveTurnstile(page, { timeout: 60, successCheck: () => pageReady(page), reloadAfter: 8, shot: 'login_dashboard_cf_fail.png' });
    console.log(`📝 当前Title: ${await page.title().catch(() => '')}`);
    if (page.url().includes('auth/login')) throw new Error('登录失败，仍停留在登录页');
    return true;
}

async function performLogin(page, acc) {
    await page.addInitScript(INJECTED_SCRIPT);

    for (let attempt = 1; attempt <= 3; attempt++) {
        console.log(`\n🔄 [尝试 ${attempt}/3] 开始登录验证...`);
        try {
            await attemptSingleLogin(page, acc);
            console.log(`✅ 第 ${attempt} 次尝试成功进入控制台！`);
            return true;
        } catch (e) {
            console.log(`⚠️ 第 ${attempt} 次登录失败: ${e.message}`);

            await page.screenshot({ path: `error_acc_${acc.id}_attempt_${attempt}.png`, fullPage: true }).catch(() => {});
            
            if (attempt === 3) throw new Error(`3 次重试后仍失败。最后报错: ${e.message}`);
            console.log('🔄 正在刷新页面重置状态...');
            await page.reload({ waitUntil: 'domcontentloaded' });
            await page.waitForTimeout(3000 + Math.random() * 2000);
        }
    }
}

module.exports = { performLogin, attemptTurnstileCdp, solveTurnstile, pageReady, challengeBoxes, tsState };

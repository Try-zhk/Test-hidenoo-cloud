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

async function attemptSingleLogin(page, acc) {
    await page.goto('https://dash.hidencloud.com/auth/login', { waitUntil: 'domcontentloaded', timeout: 30000 });
    console.log('⏳ 等待页面加载及检测 CF 盾...');

    let isCfPassed = false;
    for (let i = 0; i < 25; i++) {
        const visible = await page.getByRole('textbox', { name: 'Email or Username' }).isVisible().catch(() => false);
        if (visible) { isCfPassed = true; break; }
        await attemptTurnstileCdp(page);
        await page.waitForTimeout(2000);
    }
    if (!isCfPassed) throw new Error('无法突破初始 CF 验证盾，输入框未出现');

    console.log('✅ 页面就绪！开始填写凭据...');
    const emailBox = page.getByRole('textbox', { name: 'Email or Username' });
    const passBox  = page.getByRole('textbox', { name: 'Password' });

    await emailBox.click();
    await page.waitForTimeout(300 + Math.random() * 200);
    await emailBox.type(acc.username, { delay: 40 + Math.random() * 50 });

    await page.waitForTimeout(400 + Math.random() * 300);
    await passBox.click();
    await page.waitForTimeout(200 + Math.random() * 200);
    await passBox.type(acc.password, { delay: 40 + Math.random() * 50 });

    console.log('🛡️ 正在等待 CF 盾注入验证 Token...');
    let tokenReady = false;
    for (let j = 0; j < 15; j++) {
        const cfResponse = await page.evaluate(() => {
            const el = document.querySelector('[name="cf-turnstile-response"]');
            return el ? el.value : '';
        });
        
        if (cfResponse && cfResponse.length > 20) {
            console.log('✅ 成功获取到底层 CF Token，允许点击登录！');
            tokenReady = true;
            break;
        }
        await attemptTurnstileCdp(page);
        await page.waitForTimeout(2000); 
    }
    
    if (!tokenReady) {
        console.log('⚠️ 警告：长时间未获取到 CF Token，尝试强行登录可能会失败。');
    }

    await page.waitForTimeout(500 + Math.random() * 300);
    console.log('👆 点击登录按钮...');
    await page.getByRole('button', { name: 'Sign in to your account' }).click();

    console.log('⏳ 等待跳转控制台...');
    try {
        await page.waitForURL('**/dashboard', { timeout: 35000 });
        return true;
    } catch (_) {}

    for (let t = 0; t < 3; t++) {
        if (page.url().includes('/dashboard')) return true;
        if (await page.getByText('Incorrect password').isVisible().catch(() => false)) throw new Error('账号密码错误');
        if (await page.getByText('cf-turnstile-response field is required').isVisible().catch(() => false)) throw new Error('CF 验证失效 (Token 被拒绝)');
        
        const clicked = await attemptTurnstileCdp(page);
        await page.waitForTimeout(clicked ? 5000 : 3000);
    }

    if (page.url().includes('/dashboard')) return true;
    throw new Error('登录超时，可能遇到无反应的死盾');
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

module.exports = { performLogin };

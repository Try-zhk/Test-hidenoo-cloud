const cheerio = require('cheerio');
const crypto = require('crypto');
const { attemptTurnstileCdp } = require('./login.js');

const SLEEP = (min = 3000, max = 5000) => new Promise(r => setTimeout(r, Math.floor(Math.random() * (max - min + 1)) + min));

class RenewManager {
    constructor(page, userState, maskedUser) {
        this.page = page;
        this.maskedUser = maskedUser;
        this.state = userState;
        this.csrfToken = '';

        this.stats = { success: 0, skipped: 0, failed: 0, total: 0 };
        this.latestDueDate = 0;
    }

    log(msg) { console.log(`[${this.maskedUser}] ${msg}`); }

    async request(method, url, data = null) {
        const targetUrl = url.startsWith('http') ? url : `https://dash.hidencloud.com${url.startsWith('/') ? '' : '/'}${url}`;
        const headers = method === 'POST' ? { 'Content-Type': 'application/x-www-form-urlencoded' } : {};
        if (this.csrfToken) headers['X-CSRF-TOKEN'] = this.csrfToken;

        return await this.page.evaluate(async ({ url, method, data, headers }) => {
            const options = { method, headers, redirect: 'follow' };
            if (data) options.body = data;
            const res = await fetch(url, options);
            return { status: res.status, finalUrl: res.url, data: await res.text() };
        }, { url: targetUrl, method, data: data ? data.toString() : null, headers });
    }

    extractDate(html) {
        const $ = cheerio.load(html);
        let dueDateText = '';
        $('h6').each((i, el) => {
            if ($(el).text().trim().toLowerCase() === 'due date') {
                dueDateText = $(el).next('div').text().trim();
            }
        });
        if (dueDateText) {
            const timestamp = Date.parse(`${dueDateText} 00:00:00 GMT`);
            if (!isNaN(timestamp)) return timestamp;
        }
        return null;
    }

    // =========================================================
    // Cloudflare Turnstile 处理（新版站点在续费弹窗内新增了一道验证）
    // 复用 login.js 的 shadow-DOM 探测 + 仿人类 CDP 点击机制：
    // - 探测：挑战 iframe 在闭包 shadow DOM 里，常规选择器找不到，
    //   但 page.frames() 的浏览器层 frame 树能看到（INJECTED_SCRIPT 在其中定位复选框）
    // - 通过信号1：页面 widget 全部生成 token（cf-turnstile-response 有值）
    // - 通过信号2：挑战框被点击处理后持续消失
    // =========================================================

    async getTurnstileState() {
        const st = await this.page.evaluate(() => {
            let total = 0, solved = 0;
            document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').forEach(n => {
                total++;
                if (n.value && n.value.length > 20) solved++;
            });
            return { total, solved };
        }).catch(() => null);
        const hasCfFrame = this.page.frames().some(f => (f.url() || '').includes('challenges.cloudflare.com'));
        return { total: st ? st.total : 0, solved: st ? st.solved : 0, hasCfFrame };
    }

    async waitForPageReady(timeoutMs = 60000) {
        const blocked = ['just a moment', 'attention required', 'checking your browser', '请稍候', 'security verification'];
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            const title = ((await this.page.title().catch(() => '')) || '').toLowerCase();
            if (title && !blocked.some(k => title.includes(k))) return true;
            await attemptTurnstileCdp(this.page);
            await this.page.waitForTimeout(2000);
        }
        return false;
    }

    // 点击后长时间无 token 时重置卡住的 widget 状态，允许对同一位置重新点击
    async rearmTurnstileFrames() {
        for (const frame of this.page.frames()) {
            await frame.evaluate(() => {
                if (window.__turnstile_state === 'clicked' && window.__turnstile_data) {
                    window.__turnstile_state = 'found';
                }
            }).catch(() => {});
        }
    }

    async solveModalTurnstile(timeoutMs = 90000) {
        this.log('🛡️ 处理弹窗内的 Turnstile 验证...');
        const deadline = Date.now() + timeoutMs;
        let sawWidget = false, clickCount = 0, lastClickAt = 0;
        let frameGoneSince = 0, noWidgetSince = 0;

        while (Date.now() < deadline) {
            const st = await this.getTurnstileState();

            // 通过信号1：widget 全部生成 token
            if (st.total > 0 && st.solved >= st.total) {
                this.log(`✅ Turnstile 验证通过（token 已生成 ${st.solved}/${st.total}）！`);
                return true;
            }

            if (st.hasCfFrame || st.total > st.solved) {
                sawWidget = true;
                frameGoneSince = 0;
                noWidgetSince = 0;
            } else if (sawWidget) {
                // 通过信号2：挑战框被点击处理后持续消失
                if (!frameGoneSince) frameGoneSince = Date.now();
                else if (Date.now() - frameGoneSince >= 8000) {
                    this.log('✅ Turnstile 验证通过（挑战框已消失）！');
                    return true;
                }
            } else {
                // 弹窗内始终没出现验证组件，宽限一段时间后视为无需验证
                if (!noWidgetSince) noWidgetSince = Date.now();
                else if (Date.now() - noWidgetSince >= 10000) {
                    this.log('ℹ️ 弹窗内未出现 Turnstile，无需处理');
                    return true;
                }
            }

            const clicked = await attemptTurnstileCdp(this.page);
            if (clicked) {
                clickCount++;
                lastClickAt = Date.now();
                // 点击后 widget 会进入数秒的"验证中"状态
                await this.page.waitForTimeout(4000 + Math.random() * 2000);
                continue;
            }

            if (clickCount > 0 && Date.now() - lastClickAt >= 15000) {
                await this.rearmTurnstileFrames();
                lastClickAt = Date.now();
            }
            await this.page.waitForTimeout(2000);
        }

        this.log(`⚠️ Turnstile 处理超时（${Math.round(timeoutMs / 1000)}s）`);
        await this.page.screenshot({ path: `turnstile_modal_timeout_${Date.now()}.png`, fullPage: true }).catch(() => {});
        return false;
    }

    // =========================================================
    // 浏览器续期流程（对齐新版站点交互）：
    // 点击 Renew → 弹窗 → 通过弹窗内 Turnstile → Create Invoice → 发票页 Pay
    // 旧的直接 POST /service/{id}/renew 因缺少 turnstile token 已失效
    // 返回: true 续期支付完成 / false 失败 / 'NOT_TIME' 未到续期时间
    // =========================================================

    async renewViaBrowser(serviceId) {
        this.log('➡ 进入浏览器续期流程...');
        await this.page.goto(`https://dash.hidencloud.com/service/${serviceId}/manage`, { waitUntil: 'domcontentloaded', timeout: 60000 });
        await this.waitForPageReady(60000);

        const renewBtn = this.page.locator('button:has-text("Renew")').first();
        const createBtn = this.page.locator('button:has-text("Create Invoice")').first();

        let renewVisible = false;
        for (let i = 0; i < 30; i++) {
            if (await renewBtn.isVisible().catch(() => false)) { renewVisible = true; break; }
            await attemptTurnstileCdp(this.page);
            await this.page.waitForTimeout(2000);
        }
        if (!renewVisible) {
            this.log('❌ Renew 按钮未出现（可能被 CF 拦截页挡住）');
            await this.page.screenshot({ path: `renew_btn_missing_${serviceId}.png`, fullPage: true }).catch(() => {});
            return false;
        }

        // 点击 Renew 打开弹窗（最多 6 次），同时检测"未到续期时间"提示弹窗
        let modalOpened = false;
        for (let i = 0; i < 6; i++) {
            try {
                await renewBtn.scrollIntoViewIfNeeded().catch(() => {});
                this.log(`🖱️ 第 ${i + 1} 次尝试点击 'Renew'...`);
                await renewBtn.click({ timeout: 8000 });
            } catch (e) {
                this.log(`⚠️ 点击 Renew 出错: ${e.message}`);
                continue;
            }

            await this.page.waitForTimeout(2000);
            const bodyText = await this.page.locator('body').innerText().catch(() => '');
            if (/Renewal Restricted/i.test(bodyText) || /can only renew/i.test(bodyText)) {
                this.log('⚠️ 未到续期时间，无法续期。');
                await this.page.screenshot({ path: 'renew_not_allowed.png' }).catch(() => {});
                return 'NOT_TIME';
            }

            // 弹窗检测：Create Invoice 按钮或 Turnstile 验证组件出现
            const st = await this.getTurnstileState();
            if (await createBtn.isVisible().catch(() => false) || st.hasCfFrame || st.total > 0) {
                modalOpened = true;
                this.log('✅ 续费弹窗已弹出！');
                break;
            }
            this.log('⚠️ 弹窗未出现，可能是点击未响应，准备重试...');
            await this.page.waitForTimeout(2000);
        }
        if (!modalOpened) {
            this.log('❌ 尝试多次后，续费弹窗仍未出现。');
            await this.page.screenshot({ path: 'renew_modal_failed.png', fullPage: true }).catch(() => {});
            return false;
        }

        // ★ 新增验证：必须先通过弹窗内的 Turnstile，Create Invoice 才会生效
        if (!(await this.solveModalTurnstile(90000))) {
            this.log('⚠️ 弹窗内 Turnstile 未确认通过，仍尝试点击 Create Invoice...');
        }

        await createBtn.waitFor({ state: 'visible', timeout: 30000 }).catch(() => {});
        let createClicked = false;
        for (let i = 0; i < 3; i++) {
            try {
                this.log(`🖱️ 点击 'Create Invoice'（第 ${i + 1} 次）...`);
                await createBtn.click({ timeout: 8000 });
                createClicked = true;
                break;
            } catch (e) {
                this.log(`⚠️ 点击 'Create Invoice' 失败: ${e.message}`);
                // 可能 token 未生效，再处理一次验证
                await this.solveModalTurnstile(30000);
            }
        }
        if (!createClicked) {
            this.log("❌ 无法点击 'Create Invoice'。");
            await this.page.screenshot({ path: 'create_invoice_failed.png', fullPage: true }).catch(() => {});
            return false;
        }

        // 等待跳转到发票页（期间若再出现 CF 验证则顺手处理）
        let invoiceUrl = null;
        const waitDeadline = Date.now() + 90000;
        while (Date.now() < waitDeadline) {
            const currentUrl = this.page.url();
            if (currentUrl.includes('/payment/invoice/') || currentUrl.includes('/invoice/')) {
                invoiceUrl = currentUrl;
                this.log(`🎉 页面已跳转: ${invoiceUrl}`);
                break;
            }
            await attemptTurnstileCdp(this.page);
            await this.page.waitForTimeout(1000);
        }
        if (!invoiceUrl) {
            this.log('❌ 未能进入发票页面，超时。');
            await this.page.screenshot({ path: 'renew_stuck_invoice.png', fullPage: true }).catch(() => {});
            return false;
        }

        // 发票页支付：优先 UI 点击 Pay，找不到时回退旧的 HTTP 表单提交
        await this.waitForPageReady(60000);
        const payBtn = this.page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').first();
        let paid = false;
        try {
            await payBtn.waitFor({ state: 'visible', timeout: 30000 });
            this.log("🔎 找到 'Pay' 按钮，点击支付...");
            await payBtn.click();
            this.log("✅ 'Pay' 按钮已点击。");
            await this.page.waitForTimeout(3000);
            paid = true;
        } catch (e) {
            this.log('⚠️ 未找到可点击的 Pay 按钮，回退 HTTP 表单支付...');
            const invRes = await this.request('GET', invoiceUrl);
            paid = await this.payFromHtml(invRes.data, invoiceUrl);
        }

        // 防止 Pay 跳去第三方域名，导致后续页内 fetch 跨域失效
        if (!this.page.url().includes('dash.hidencloud.com')) {
            await this.page.goto('https://dash.hidencloud.com/dashboard', { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
        }
        return paid;
    }

    async execute() {
        this.log('🔍 初始化 API 状态...');
        await SLEEP(2000, 3000);
        const dashRes = await this.request('GET', '/dashboard');

        if (dashRes.finalUrl.includes('/login')) throw new Error('登录态异常失效');

        const $ = cheerio.load(dashRes.data);
        if ($('title').text().trim().includes('Just a moment')) throw new Error('遇到拦截页面');

        this.csrfToken = $('meta[name="csrf-token"]').attr('content') || '';

        const services = [];
        $('a[href*="/service/"]').each((i, el) => {
            const match = $(el).attr('href').match(/\/service\/(\d+)\/manage/);
            if (match) services.push(match[1]);
        });
        const uniqueServices = [...new Set(services)];
        this.stats.total = uniqueServices.length;

        this.log(`✅ 发现 ${uniqueServices.length} 个服务`);

        for (const svcId of uniqueServices) {
            const finalSvcDate = await this.processService(svcId);
            if (finalSvcDate && finalSvcDate > this.latestDueDate) {
                this.latestDueDate = finalSvcDate;
            }
        }

        return { stats: this.stats, newState: this.state, latestDueDate: this.latestDueDate === 0 ? null : this.latestDueDate };
    }

    async processService(serviceId) {
        await SLEEP(2000, 3000);

        const svcHash = crypto.createHash('md5').update(String(serviceId)).digest('hex').substring(0, 8);
        this.log(`>>> 处理服务: [Hash-${svcHash}]`);

        const res = await this.request('GET', `/service/${serviceId}/manage`);
        const $ = cheerio.load(res.data);

        const parsedDate = this.extractDate(res.data);
        if (parsedDate) this.state[svcHash] = parsedDate;

        let needsRenew = true;
        if (this.state[svcHash]) {
            if ((this.state[svcHash] - Date.now()) > 86400000) {
                this.log(`⏭️ 剩余时间 > 24H，无需续期。`);
                this.stats.skipped++;
                needsRenew = false;
            }
        }

        if (needsRenew) {
            this.log(`📅 临近到期，开始续期流程...`);
            const result = await this.renewViaBrowser(serviceId);

            let isPaid = false;
            if (result === 'NOT_TIME') {
                this.stats.skipped++;
                return this.state[svcHash];
            } else if (result === true) {
                isPaid = true;
            } else {
                this.log('⚠️ 浏览器续期未完成，检查未支付账单...');
                isPaid = await this.checkUnpaidInvoices(serviceId);
            }

            if (isPaid) {
                this.stats.success++;
                this.log(`🔄 支付成功，重新刷新页面获取最新到期日...`);
                await SLEEP(2000, 3000);
                const refreshRes = await this.request('GET', `/service/${serviceId}/manage`);
                const newDate = this.extractDate(refreshRes.data);
                if (newDate) {
                    this.state[svcHash] = newDate;
                }
            } else {
                this.stats.failed++;
            }
        }

        return this.state[svcHash];
    }

    async checkUnpaidInvoices(serviceId) {
        await SLEEP(1500, 2500);
        const res = await this.request('GET', `/service/${serviceId}/invoices?where=unpaid`);
        const $ = cheerio.load(res.data);
        const urls = new Set();
        $('a[href*="/invoice/"]').each((i, el) => {
            const href = $(el).attr('href');
            if (!href.includes('download')) urls.add(href);
        });

        if (urls.size === 0) {
            this.log(`⚪ 无未支付账单`);
            return false;
        }

        let paidAny = false;
        for (const url of urls) {
            this.log(`📄 打开并支付系统生成的账单...`);
            const invRes = await this.request('GET', url);
            const success = await this.payFromHtml(invRes.data, url);
            if (success) paidAny = true;
            await SLEEP(2000, 3000);
        }
        return paidAny;
    }

    async payFromHtml(html, url) {
        const $ = cheerio.load(html);
        let targetForm = null, action = '';

        $('form').each((i, form) => {
            const btnText = $(form).find('button').text().trim().toLowerCase();
            const act = $(form).attr('action');
            if (btnText.includes('pay') && act && !act.includes('balance/add')) {
                targetForm = $(form);
                action = act;
                return false;
            }
        });

        if (!targetForm) {
            this.log(`⚪ 页面未找到支付表单 (可能已支付)`);
            return true;
        }

        const params = new URLSearchParams();
        targetForm.find('input').each((i, el) => {
            const name = $(el).attr('name');
            if (name) params.append(name, $(el).val() || '');
        });

        this.log(`💳 提交支付...`);
        const res = await this.request('POST', action, params.toString());
        if (res.status === 200) {
            this.log(`✅ 支付成功！`);
            return true;
        } else {
            this.log(`⚠️ 支付响应异常: ${res.status}`);
            return false;
        }
    }
}

module.exports = { RenewManager };

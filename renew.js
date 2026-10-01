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

    fullUrl(url) {
        return url.startsWith('http') ? url : `https://dash.hidencloud.com${url.startsWith('/') ? '' : '/'}${url}`;
    }

    // 整页 CF 拦截检测（Turnstile 组件常驻在正常页面里，不算拦截）
    async onChallenge() {
        try {
            return await this.page.evaluate(() => {
                const title = document.title || '';
                const body = document.body ? document.body.innerText.slice(0, 600) : '';
                const interstitial = /just a moment|checking your browser|attention required|请稍候/i.test(title);
                const ids = !!document.querySelector('#challenge-form, #challenge-running, #challenge-error-text');
                const gate = /verify you are human|checking if the site connection is secure|enable javascript and cookies/i.test(body)
                    && !body.includes('Due date');
                return interstitial || ids || gate;
            });
        } catch (e) { return true; }
    }

    async waitCf(maxMs = 90000) {
        if (!(await this.onChallenge())) return true;
        this.log('🛡️ 检测到 CF 验证页，等待通过...');
        const deadline = Date.now() + maxMs;
        while (Date.now() < deadline) {
            await attemptTurnstileCdp(this.page);
            await this.page.waitForTimeout(2000);
            if (!(await this.onChallenge())) { this.log('✅ CF 验证已通过'); return true; }
        }
        return false;
    }

    // 用真实页面导航代替 fetch，返回 { finalUrl, data }
    async open(url) {
        await this.page.goto(this.fullUrl(url), { waitUntil: 'domcontentloaded', timeout: 45000 });
        await this.page.waitForTimeout(3000);
        if (!(await this.waitCf())) throw new Error('遇到拦截页面 (CF 验证超时)');
        return { finalUrl: this.page.url(), data: await this.page.content() };
    }

    extractDate(html) {
        const $ = cheerio.load(html);
        let dueDateText = '';
        $('h6').each((i, el) => {
            if ($(el).text().trim().toLowerCase() === 'due date') {
                dueDateText = $(el).next('div').text().trim();
            }
        });
        if (!dueDateText) {
            const t = $('body').text().replace(/\s+/g, ' ');
            const m = t.match(/Due date\s*(\d{1,2}\s+[A-Za-z]{3}[A-Za-z]*\s+\d{4})/i);
            if (m) dueDateText = m[1];
        }
        this.log(`🔎 [调试] 页面抓到的 Due Date 原文: "${dueDateText}"`);
        const MONTHS = { jan:0, feb:1, mar:2, apr:3, may:4, jun:5, jul:6, aug:7, sep:8, oct:9, nov:10, dec:11 };
        const match = dueDateText.match(/(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{4})/);
        if (match) {
            const month = MONTHS[match[2].toLowerCase()];
            if (month !== undefined) {
                const ts = Date.UTC(+match[3], month, +match[1]);
                this.log(`🔎 [调试] 解析结果: ${new Date(ts).toISOString()}`);
                return ts;
            }
        }
        this.log(`🔎 [调试] 解析失败，正则未匹配到日期`);
        return null;
    }

    async execute() {
        this.log('🔍 初始化 API 状态...');
        await SLEEP(2000, 3000);
        const dashRes = await this.open('/dashboard');

        if (dashRes.finalUrl.includes('/login')) throw new Error('登录态异常失效');

        const $ = cheerio.load(dashRes.data);

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
        this.log(`>>> 处理服务: [Hash-${svcHash}] [原始ID:${serviceId}]`);

        const res = await this.open(`/service/${serviceId}/manage`);
        if (res.finalUrl.includes('/login')) throw new Error('登录态异常失效');

        const parsedDate = this.extractDate(res.data);
        if (parsedDate) this.state[svcHash] = parsedDate;

        let finalDate = parsedDate;
        if (parsedDate) {
            const remainHours = ((parsedDate - Date.now()) / 3600000).toFixed(1);
            this.log(`🔎 [调试] 剩余时间: ${remainHours} 小时`);
            if ((parsedDate - Date.now()) > 86400000) {
                this.log(`⏭️ 剩余时间 > 24H，无需续期。`);
                this.stats.skipped++;
                return finalDate;
            }
        }

        this.log(`📅 进入续期流程...`);
        const renewResult = await this.renewService(serviceId);

        if (renewResult === 'NOT_TIME') {
            this.log('⏭️ Renewal Restricted: 未到续期窗口，等下次运行。');
            this.stats.skipped++;
            return finalDate;
        }

        if (renewResult) {
            this.stats.success++;
            this.log(`🔄 续期完成，重新刷新页面获取最新到期日...`);
            await SLEEP(2000, 3000);
            const refreshRes = await this.open(`/service/${serviceId}/manage`);
            const newDate = this.extractDate(refreshRes.data);
            if (newDate) {
                this.state[svcHash] = newDate;
                finalDate = newDate;
            }
        } else {
            this.stats.failed++;
        }

        return finalDate;
    }

    // ===== Turnstile 辅助（流程对应 Auto-Renew-HidenCloud/app.py）=====
    async tsState() {
        try {
            return await this.page.evaluate(() => {
                let total = 0, solved = 0;
                document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').forEach(n => {
                    total++;
                    if (n.value && n.value.length > 20) solved++;
                });
                return { total, solved };
            });
        } catch (e) { return { total: 0, solved: 0 }; }
    }

    cfFrameCount() {
        try { return this.page.frames().filter(f => (f.url() || '').includes('challenges.cloudflare.com')).length; }
        catch (e) { return 0; }
    }

    // 通过信号: successCheck 成立 / 出现新 token 且全部 widget 已解决 / 挑战框出现后消失 8s
    // requirePositive=true 时，页面没出现 Turnstile 不会提前返回
    async solveTurnstile(timeoutSec = 90, requirePositive = false, successCheck = null, shot = 'turnstile_timeout.png') {
        this.log('🛡️ 处理 Turnstile...');
        const start = Date.now();
        const base = await this.tsState();
        let hadFrame = false, goneSince = null;

        while (Date.now() - start < timeoutSec * 1000) {
            if (successCheck) {
                try { if (await successCheck()) { this.log('✅ Turnstile 处理完成'); return true; } } catch (e) {}
            }
            const st = await this.tsState();
            if (st.total > 0 && st.solved >= st.total && (st.total > base.total || st.solved > base.solved)) {
                this.log(`✅ Turnstile 验证通过（token ${st.solved}/${st.total}）`);
                return true;
            }

            if (this.cfFrameCount() > 0) {
                hadFrame = true;
                goneSince = null;
                await attemptTurnstileCdp(this.page);
                await this.page.waitForTimeout(2000 + Math.random() * 1500);
            } else {
                if (hadFrame) {
                    if (!goneSince) goneSince = Date.now();
                    else if (Date.now() - goneSince >= 8000) { this.log('✅ Turnstile 验证通过（挑战框已消失）'); return true; }
                } else if (!requirePositive && !successCheck && Date.now() - start >= 5000) {
                    this.log('ℹ️ 页面未出现 Turnstile，无需处理');
                    return true;
                }
                await this.page.waitForTimeout(1000);
            }
        }
        this.log(`❌ Turnstile 处理超时（${timeoutSec}s）`);
        await this.page.screenshot({ path: shot }).catch(() => {});
        return false;
    }

    // 返回 'NOT_TIME' / true / false
    async renewService(serviceId) {
        const svcPath = `/service/${serviceId}/manage`;
        try {
            if (!this.page.url().includes(svcPath)) await this.open(svcPath);

            const renewBtn = this.page.locator('button:has-text("Renew")').first();
            const createBtn = this.page.locator('button:has-text("Create Invoice")').first();

            // 1. 点击 Renew，最多重试 6 次
            let modalOpened = false;
            for (let i = 0; i < 6; i++) {
                try {
                    await renewBtn.waitFor({ state: 'visible', timeout: 10000 });
                    await renewBtn.scrollIntoViewIfNeeded();
                    this.log(`🖱️ 第 ${i + 1} 次尝试点击 'Renew'...`);
                    await renewBtn.click();

                    await this.page.waitForTimeout(2000);
                    const pageText = await this.page.locator('body').innerText();
                    if (pageText.includes('Renewal Restricted') || pageText.toLowerCase().includes('can only renew')) {
                        await this.page.screenshot({ path: 'renew_not_allowed.png' }).catch(() => {});
                        return 'NOT_TIME';
                    }

                    this.log('🖲️ 等待弹窗出现...');
                    try {
                        await createBtn.waitFor({ state: 'visible', timeout: 5000 });
                        modalOpened = true;
                        this.log('✅ 弹窗已成功弹出！');
                        break;
                    } catch (e) {
                        // 弹窗可能先展示 Turnstile，Create Invoice 稍后才出现
                        if (this.cfFrameCount() > 0) {
                            modalOpened = true;
                            this.log('✅ 弹窗已弹出（先出现 Turnstile 验证）！');
                            break;
                        }
                        this.log('⚠️ 弹窗未出现，可能是点击未响应，准备重试...');
                        await this.page.waitForTimeout(2000);
                    }
                } catch (e) {
                    this.log(`❌ 点击尝试出错: ${e.message.split('\n')[0]}`);
                }
            }
            if (!modalOpened) {
                this.log('❌ 尝试多次后，续费弹窗仍未出现。');
                await this.page.screenshot({ path: 'renew_modal_failed.png' }).catch(() => {});
                return false;
            }

            // 2. 弹窗内的 Turnstile：处理完再点 Create Invoice
            this.log('🛡️ 处理弹窗内的 Turnstile...');
            if (!(await this.solveTurnstile(90, true, null, 'modal_turnstile_fail.png'))) {
                this.log("⚠️ 弹窗内 Turnstile 未确认通过，仍尝试点击 'Create Invoice'...");
            }

            await createBtn.waitFor({ state: 'visible', timeout: 30000 }).catch(() => {});

            // 3. 点击 Create Invoice，最多 3 次
            let createClicked = false;
            for (let i = 0; i < 3; i++) {
                try {
                    this.log(`🖱️ 点击 'Create Invoice'（第 ${i + 1} 次）...`);
                    await createBtn.click({ timeout: 8000 });
                    createClicked = true;
                    break;
                } catch (e) {
                    this.log(`⚠️ 点击 'Create Invoice' 失败: ${e.message.split('\n')[0]}`);
                    await this.solveTurnstile(30, true);
                }
            }
            if (!createClicked) {
                this.log("❌ 无法点击 'Create Invoice'。");
                await this.page.screenshot({ path: 'create_invoice_failed.png' }).catch(() => {});
                return false;
            }

            // 4. 等待跳转到账单页（最多 90s，期间遇到 Turnstile 就处理）
            let invoiceUrl = null;
            const t0 = Date.now();
            while (Date.now() - t0 < 90000) {
                if (this.page.url().includes('/payment/invoice/')) { invoiceUrl = this.page.url(); break; }
                if (this.cfFrameCount() > 0) {
                    this.log('⚠️ 遇到拦截，尝试处理...');
                    await this.solveTurnstile(45, false, () => this.page.url().includes('/payment/invoice/'));
                    continue;
                }
                await this.page.waitForTimeout(1000);
            }

            if (!invoiceUrl) {
                this.log('⚠️ 未能进入发票页面，检查未支付账单...');
                await this.page.screenshot({ path: 'renew_stuck_invoice.png' }).catch(() => {});
                return await this.checkUnpaidInvoices(serviceId);
            }

            this.log(`🎉 页面已跳转: ${invoiceUrl}`);
            if (!(await this.waitCf())) throw new Error('账单页遇到拦截页面 (CF 验证超时)');
            await this.page.waitForTimeout(2000);

            // 5. 支付
            return await this.payOnPage();
        } catch (e) {
            this.log(`❌ 续费异常: ${e.message.split('\n')[0]}`);
            await this.page.screenshot({ path: 'renew_error.png' }).catch(() => {});
            return false;
        }
    }

    // 在当前账单页点击 Pay（仅当所有 € 金额为 0 时）
    async payOnPage() {
        const text = await this.page.evaluate(() => document.body.innerText);
        const amounts = [...text.matchAll(/€\s*([\d][\d.,]*)/g)]
            .map(m => Number(m[1].replace(/[.,](?=\d{3}\b)/g, '').replace(',', '.')))
            .filter(n => Number.isFinite(n));
        if (amounts.some(n => n > 0)) { this.log('⚠️ 账单含非零金额，拒绝自动支付'); return false; }

        this.log('🔎 查找 Pay 按钮...');
        const payBtn = this.page.locator('a:has-text("Pay"):visible, button:has-text("Pay"):visible').first();
        try {
            await payBtn.waitFor({ state: 'visible', timeout: 30000 });
        } catch (e) {
            this.log(`⚪ 未找到 Pay 按钮 (可能已支付)`);
            await this.page.screenshot({ path: 'pay_btn_not_found.png' }).catch(() => {});
            return /payment has been completed|paid/i.test(text);
        }
        this.log(`💳 点击 Pay...`);
        await payBtn.click();
        this.log(`✅ 'Pay' 按钮已点击。`);
        await this.page.waitForTimeout(5000);
        return true;
    }

    async checkUnpaidInvoices(serviceId) {
        await SLEEP(1500, 2500);
        const res = await this.open(`/service/${serviceId}/invoices?where=unpaid`);
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
            await this.open(url);
            if (await this.payOnPage()) paidAny = true;
            await SLEEP(2000, 3000);
        }
        return paidAny;
    }
}

module.exports = { RenewManager };

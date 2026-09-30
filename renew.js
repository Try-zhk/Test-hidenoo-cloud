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

        this.log(`📅 点击 Renew...`);
        const renewBtn = this.page.getByRole('button', { name: 'Renew' });
        if ((await renewBtn.count()) !== 1) {
            this.log('⚠️ 未找到唯一的 Renew 按钮（续期窗口未开放或页面改版）');
            this.stats.failed++;
            return finalDate;
        }
        await renewBtn.click();

        // 两种结果: 续期弹窗(Create Invoice) / Renewal Restricted
        let outcome = null;
        for (let i = 0; i < 10 && !outcome; i++) {
            await this.page.waitForTimeout(1500);
            try {
                if (await this.page.getByText('Renewal Restricted').count()) outcome = 'restricted';
                else if ((await this.page.getByRole('dialog').count()) === 1) {
                    const t = await this.page.getByRole('dialog').innerText();
                    if (t.includes('Create Invoice')) outcome = 'offer';
                }
            } catch (e) {}
        }

        if (outcome === 'restricted') {
            this.log('⏭️ Renewal Restricted: 未到续期窗口，等下次运行。');
            this.stats.skipped++;
            return finalDate;
        }
        if (outcome !== 'offer') {
            this.log('⚠️ 点击 Renew 后未出现续期弹窗');
            this.stats.failed++;
            return finalDate;
        }

        const dialog = this.page.getByRole('dialog');
        const dialogText = await dialog.innerText();
        if (!dialogText.includes('€0.00')) {
            this.log('⚠️ 续期弹窗不是免费续期，已中止');
            this.stats.failed++;
            return finalDate;
        }

        await dialog.getByRole('button', { name: 'Create Invoice' }).click();
        let isPaid = false;
        try {
            await this.page.waitForURL(/\/payment\/invoice\//, { timeout: 45000 });
            this.log(`⚡️ 账单已生成，前往支付`);
            await this.page.waitForTimeout(2000);
            isPaid = await this.payOnPage();
        } catch (e) {
            this.log('⚠️ 未跳转到账单页，检查未支付账单...');
            isPaid = await this.checkUnpaidInvoices(serviceId);
        }

        if (isPaid) {
            this.stats.success++;
            this.log(`🔄 支付成功，重新刷新页面获取最新到期日...`);
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

    // 在当前账单页点击 Pay（仅当所有 € 金额为 0 时）
    async payOnPage() {
        const text = await this.page.evaluate(() => document.body.innerText);
        const amounts = [...text.matchAll(/€\s*([\d][\d.,]*)/g)]
            .map(m => Number(m[1].replace(/[.,](?=\d{3}\b)/g, '').replace(',', '.')))
            .filter(n => Number.isFinite(n));
        if (amounts.some(n => n > 0)) { this.log('⚠️ 账单含非零金额，拒绝自动支付'); return false; }

        const payBtn = this.page.getByRole('button', { name: 'Pay', exact: true });
        if ((await payBtn.count()) !== 1) {
            this.log(`⚪ 未找到 Pay 按钮 (可能已支付)`);
            return /payment has been completed|paid/i.test(text);
        }
        this.log(`💳 点击 Pay...`);
        await payBtn.click();
        try {
            await this.page.getByText(/payment has been completed/i).waitFor({ state: 'visible', timeout: 45000 });
            this.log(`✅ 支付成功！`);
            return true;
        } catch (e) {
            this.log(`⚠️ 未检测到支付成功提示`);
            return false;
        }
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

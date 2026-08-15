const cheerio = require('cheerio');
const crypto = require('crypto');

const RENEW_DAYS = 10;
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
        const MONTHS = { jan:0, feb:1, mar:2, apr:3, may:4, jun:5, jul:6, aug:7, sep:8, oct:9, nov:10, dec:11 };
        const match = dueDateText.match(/(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{4})/);
        if (match) {
            const month = MONTHS[match[2].toLowerCase()];
            if (month !== undefined) {
                return Date.UTC(+match[3], month, +match[1]);
            }
        }
        return null;
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
        const formToken = $('input[name="_token"]').val();
        
        const parsedDate = this.extractDate(res.data);
        if (parsedDate) this.state[svcHash] = parsedDate;

        let finalDate = parsedDate;
        let needsRenew = true;
        if (parsedDate) {
            if ((parsedDate - Date.now()) > 86400000) {
                this.log(`⏭️ 剩余时间 > 24H，无需续期。`);
                this.stats.skipped++;
                needsRenew = false;
            }
        }

        if (needsRenew) {
            this.log(`📅 提交续期 (${RENEW_DAYS}天)...`);
            const params = new URLSearchParams({ _token: formToken, days: RENEW_DAYS });
            const renewRes = await this.request('POST', `/service/${serviceId}/renew`, params.toString());
            
            let isPaid = false;
            if (renewRes.finalUrl && renewRes.finalUrl.includes('/invoice/')) {
                this.log(`⚡️ 续期成功，前往支付`);
                isPaid = await this.payFromHtml(renewRes.data, renewRes.finalUrl);
            } else {
                this.log('⚠️ 续期未直接跳转，检查未支付账单...');
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
                    finalDate = newDate;
                }
            } else {
                this.stats.failed++;
            }
        }

        return finalDate;
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

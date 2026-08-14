const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();
const { spawn, execSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const axios = require('axios');
const nodemailer = require('nodemailer');

const { performLogin } = require('./login.js');
const { RenewManager } = require('./renew.js');

chromium.use(stealth);

const STATE_FILE = './state.json';
const CHROME_PATH = process.env.CHROME_PATH || '/usr/bin/google-chrome';
const DEBUG_PORT = 9222;

function maskEmail(email) {
    if (!email || !email.includes('@')) return '***';
    const [name, domain] = email.split('@');
    const maskedName = name.length > 3 ? name.substring(0, 3) + '***' : name + '***';
    const tld = domain.includes('.') ? domain.split('.').pop() : 'com';
    return `${maskedName}@***.${tld}`;
}

function maskIP(ip) {
    if (!ip) return '***.***.***.***';
    const parts = ip.trim().split('.');
    return parts.length === 4 ? `${parts[0]}.${parts[1]}.***.***` : '***';
}

// 到期时间精确到秒，用于 TG/邮件通知（明文）
function formatDate(timestamp) {
    if (!timestamp) return '未知';
    const d = new Date(timestamp);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function getAccounts() {
    const accounts = [];
    for (const key in process.env) {
        const match = key.match(/^HIDEN_ACCOUNT_(\d+)$/);
        if (match) {
            const id = match[1];
            const [username, password] = (process.env[key] || '').trim().split(/\s+/);
            if (username && password) {
                accounts.push({
                    id, username, password,
                    proxyUrl: process.env[`PROXY_URL_${id}`] || null,
                    proxyLock: process.env[`PROXY_LOCK_${id}`] !== 'false',
                    cookies: process.env[`HIDEN_COOKIES_${id}`] || null
                });
            }
        }
    }
    return accounts;
}

function getSmtpConfig() {
    const str = process.env.SMTP_CONFIG;
    if (!str) return null;
    try { return JSON.parse(str.trim()); } catch (e) {}
    try { const obj = eval(`(${str.trim()})`); if (obj && obj.host) return obj; } catch (e) {}
    try {
        const hostMatch = str.match(/host['"]?\s*:\s*['"]([^'"]+)['"]/i) || str.match(/host\s*:\s*([^,\s}]+)/i);
        const portMatch = str.match(/port['"]?\s*:\s*(\d+)/i);
        const userMatch = str.match(/user['"]?\s*:\s*['"]([^'"]+)['"]/i) || str.match(/user\s*:\s*([^,\s}]+)/i);
        const passMatch = str.match(/pass['"]?\s*:\s*['"]([^'"]+)['"]/i) || str.match(/pass\s*:\s*([^,\s}]+)/i);

        if (hostMatch && userMatch && passMatch) {
            return { host: hostMatch[1], port: portMatch ? parseInt(portMatch[1]) : 587, user: userMatch[1], pass: passMatch[1] };
        }
    } catch (e) {}
    return null;
}

async function saveCookieToGitHub(id, cookiesArr) {
    if (!process.env.GH_PAT || !process.env.GITHUB_REPO) return;
    try {
        const varName = `HIDEN_COOKIES_${id}`;
        const headers = { 'Accept': 'application/vnd.github+json', 'Authorization': `Bearer ${process.env.GH_PAT}`, 'X-GitHub-Api-Version': '2022-11-28' };
        const apiUrl = `https://api.github.com/repos/${process.env.GITHUB_REPO}/actions/variables`;
        let exists = false;
        try { await axios.get(`${apiUrl}/${varName}`, { headers }); exists = true; } catch (e) {}
        if (exists) await axios.patch(`${apiUrl}/${varName}`, { name: varName, value: JSON.stringify(cookiesArr) }, { headers });
        else await axios.post(apiUrl, { name: varName, value: JSON.stringify(cookiesArr) }, { headers });
        console.log(`✅ 已自动保存 Cookie 至变量: ${varName}`);
    } catch (e) { console.error(`❌ 保存 Cookie 失败:`, e.message); }
}

// TG / 邮件通知：明文账号 + 明文出口IP + 精确到秒的到期时间
async function sendNotifications(summaryArr) {
    let mdText = `☁️ *HidenCloud 自动续期报告*\n━━━━━━━━━━━━━━━━━━\n`;
    let htmlText = `<div style="font-family: Arial, sans-serif; max-width: 650px; margin: auto; border: 1px solid #e0e0e0; border-radius: 10px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
        <div style="background-color: #2c3e50; padding: 20px; text-align: center;">
            <h2 style="color: #ffffff; margin: 0; font-size: 24px;">☁️ HidenCloud 自动续期</h2>
        </div>
        <div style="padding: 20px; background-color: #fcfcfc;">`;

    summaryArr.forEach(s => {
        mdText += `👤 **账号**: \`${s.user}\`\n`;
        mdText += `🌐 **出口IP**: \`${s.ip || '未知'}\`\n`;
        mdText += `🔑 **登录**: ${s.loginMethod}\n`;
        if (s.status.includes('Failed')) {
            mdText += `❌ **异常**: ${s.status}\n`;
        } else {
            mdText += `⚡ **续期**: ${s.stats.success} 成功 / ${s.stats.skipped} 未到期 / ${s.stats.failed} 失败\n`;
            mdText += `📅 **到期**: ${formatDate(s.latestDate)}\n`;
        }
        mdText += `━━━━━━━━━━━━━━━━━━\n`;

        htmlText += `<div style="background: #ffffff; padding: 15px; border-radius: 8px; margin-bottom: 15px; border-left: 5px solid ${s.status.includes('Failed') ? '#e74c3c' : '#2ecc71'}; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
            <p style="margin: 5px 0; font-size: 16px;">👤 <b>账号:</b> ${s.user}</p>
            <p style="margin: 5px 0; font-size: 15px; color: #7f8c8d;">🌐 <b>出口IP:</b> ${s.ip || '未知'}</p>
            <p style="margin: 5px 0; font-size: 15px; color: #7f8c8d;">🔑 <b>登录:</b> ${s.loginMethod}</p>`;

        if (s.status.includes('Failed')) {
            htmlText += `<p style="margin: 5px 0; font-size: 15px; color: #e74c3c;">❌ <b>异常:</b> ${s.status}</p>`;
        } else {
            htmlText += `<p style="margin: 5px 0; font-size: 15px;">⚡ <b>续期:</b> 
                <span style="color: #27ae60; font-weight: bold;">${s.stats.success} 成功</span> / 
                <span style="color: #f39c12;">${s.stats.skipped} 未到期</span> / 
                <span style="color: #c0392b;">${s.stats.failed} 失败</span>
            </p>
            <p style="margin: 5px 0; font-size: 15px;">📅 <b>最新到期:</b> <span style="color: #2980b9; font-weight: bold;">${formatDate(s.latestDate)}</span></p>`;
        }
        htmlText += `</div>`;
    });
    htmlText += `</div></div>`;

    if (process.env.TG_TOKEN && process.env.TG_CHAT) {
        try { await axios.post(`https://api.telegram.org/bot${process.env.TG_TOKEN}/sendMessage`, { chat_id: process.env.TG_CHAT, text: mdText, parse_mode: 'Markdown' }); } catch (e) {}
    }

    const smtp = getSmtpConfig();
    if (smtp && process.env.EMAIL_CHAT) {
        try {
            const isSecure = (smtp.port === 465); 
            const transporter = nodemailer.createTransport({
                host: smtp.host, port: smtp.port, secure: isSecure, requireTLS: !isSecure,
                auth: { user: smtp.user, pass: smtp.pass },
                tls: { rejectUnauthorized: false }
            });
            await transporter.sendMail({
                from: `"HidenCloud" <${smtp.user}>`,
                to: process.env.EMAIL_CHAT,
                subject: "☁️ HidenCloud 自动续期报告",
                html: htmlText
            });
        } catch (e) { console.error('❌ 邮件发送异常。'); }
    }
}

(async () => {
    const accounts = getAccounts();
    if (accounts.length === 0) return console.log('❌ 未检测到任何 HIDEN_ACCOUNT_X 环境变量');

    let globalState = {};
    if (fs.existsSync(STATE_FILE)) globalState = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));

    const summary = [];

    for (const acc of accounts) {
        const maskedUsername = maskEmail(acc.username); // workflow/控制台日志继续脱敏
        const accKey = `ACCOUNT_${acc.id}`;
        console.log(`\n===========================================`);
        console.log(`▶ 开始处理账号: ${maskedUsername} (ID: ${acc.id})`);
        
        let singBoxProcess = null, useProxy = false;
        let currentLoginMethod = '未知';
        let exitIp = null; // 出口IP，明文只进 summary/通知，日志里脱敏打印

        if (acc.proxyUrl) {
            console.log(`🌐 解析代理 PROXY_URL_${acc.id}...`);
            try {
                process.env.PROXY_URL = acc.proxyUrl;
                execSync('node proxyurl.js', { stdio: 'pipe' });
                const logStream = fs.openSync(`./singbox_${acc.id}.log`, 'a');
                singBoxProcess = spawn('./sing-box', ['run', '-c', 'config.json'], { detached: true, stdio: ['ignore', logStream, logStream] });
                singBoxProcess.unref();
                await new Promise(r => setTimeout(r, 3000));
                useProxy = true;
                console.log(`✅ 代理本地映射成功 (127.0.0.1:8080)`);
            } catch (e) {
                if (acc.proxyLock) {
                    console.log(`🚫 PROXY_LOCK 开启，放弃执行当前账号！`);
                    summary.push({ user: acc.username, loginMethod: '未登录', status: 'Failed (代理失效)', stats: {}, latestDate: null, ip: null });
                    continue; 
                }
            }
        }

        if (!globalState[accKey]) globalState[accKey] = {};
        const userDataDir = path.join(os.tmpdir(), `chrome_data_${acc.id}`);
        const args = [`--remote-debugging-port=${DEBUG_PORT}`, '--no-first-run', '--disable-gpu', '--window-size=1280,720', '--no-sandbox', `--user-data-dir=${userDataDir}`];
        if (useProxy) args.push('--proxy-server=http://127.0.0.1:8080');

        let browser, chromeProcess, page;

        try {
            try { execSync(`pkill -f "remote-debugging-port=${DEBUG_PORT}" || true`); } catch(e){}
            chromeProcess = spawn(CHROME_PATH, args, { detached: true, stdio: 'ignore' });
            chromeProcess.unref();

            let ready = false;
            for (let k = 0; k < 20; k++) {
                try { await axios.get(`http://localhost:${DEBUG_PORT}/json/version`, { timeout: 1000 }); ready = true; break; } 
                catch(e) { await new Promise(r => setTimeout(r, 1000)); }
            }
            if (!ready) throw new Error('Chrome 启动超时');

            browser = await chromium.connectOverCDP(`http://localhost:${DEBUG_PORT}`);
            page = await browser.contexts()[0].newPage();
            page.setDefaultTimeout(60000);

            console.log('🔍 验证连通性...');
            try {
                await page.goto('https://api.ipify.org', { timeout: 20000 });
                exitIp = (await page.innerText('body')).trim();
                console.log(`✅ 网络就绪，出口 IP: ${maskIP(exitIp)}`); // 日志继续脱敏
            } catch (e) { throw new Error(`网络不可达或代理断流`); }

            let loginSuccess = false;
            if (acc.cookies) {
                console.log('🍪 发现历史 Cookie，尝试免密登录...');
                try {
                    await page.context().addCookies(JSON.parse(acc.cookies));
                    await page.goto('https://dash.hidencloud.com/dashboard', { waitUntil: 'domcontentloaded', timeout: 30000 });
                    if (!page.url().includes('/login')) {
                        console.log('✅ Cookie 依然有效，免密登录成功！');
                        loginSuccess = true;
                        currentLoginMethod = '免密 (Cookie)';
                    } else { console.log('⚠️ Cookie 已失效，进入常规密码登录...'); }
                } catch (e) { console.log('⚠️ Cookie 解析失败，进入常规登录'); }
            }

            if (!loginSuccess) {
                await performLogin(page, acc);
                console.log('✅ 账号密码登录成功！获取最新 Cookie...');
                currentLoginMethod = '密码验证 + CF盾';
                await saveCookieToGitHub(acc.id, await page.context().cookies());
            }

            const manager = new RenewManager(page, globalState[accKey], maskedUsername);
            const res = await manager.execute();

            globalState[accKey] = res.newState;
            summary.push({ user: acc.username, loginMethod: currentLoginMethod, status: 'Success', stats: res.stats, latestDate: res.latestDueDate, ip: exitIp });

        } catch (e) {
            console.error(`❌ 异常: ${e.message}`);
            if (page) await page.screenshot({ path: `error_acc_${acc.id}_FINAL.png`, fullPage: true }).catch(()=>{});
            summary.push({ user: acc.username, loginMethod: currentLoginMethod, status: `Failed: ${e.message}`, stats: {}, latestDate: null, ip: exitIp });
        } finally {
            console.log('🧹 清理环境...');
            try { if (browser) await browser.close(); } catch(e){}
            try { execSync(`pkill -f "remote-debugging-port=${DEBUG_PORT}" || true`); } catch(e){}
            if (singBoxProcess && singBoxProcess.pid) {
                try { process.kill(-singBoxProcess.pid); } catch(e) { try { execSync('pkill -f "sing-box run" || true'); } catch(err){} }
            }
            if (fs.existsSync('config.json')) fs.unlinkSync('config.json');
        }
    }

    fs.writeFileSync(STATE_FILE, JSON.stringify(globalState, null, 2));
    await sendNotifications(summary);
})();

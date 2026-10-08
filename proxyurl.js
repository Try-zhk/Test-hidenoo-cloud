#!/usr/bin/env node
/**
 * proxyurl.js -- Parse PROXY_URL and generate sing-box config.json
 * Supported protocols: vless, vmess, socks5, http, https, hy2, tuic, ss, trojan, anytls
 * Enhanced features: Auto-detect and decode Base64 encoded single/multi-line subscriptions.
 */

const fs = require('fs');

const LISTEN_HOST = "127.0.0.1";
const LISTEN_PORT = 8080;

function parseSocks5(parsed) {
    const outbound = { type: "socks", tag: "proxy", server: parsed.hostname, server_port: parseInt(parsed.port || 1080), version: "5" };
    if (parsed.username) outbound.username = decodeURIComponent(parsed.username);
    if (parsed.password) outbound.password = decodeURIComponent(parsed.password);
    return outbound;
}

function parseHttp(parsed) {
    const outbound = { type: "http", tag: "proxy", server: parsed.hostname, server_port: parseInt(parsed.port || 8080) };
    if (parsed.username) outbound.username = decodeURIComponent(parsed.username);
    if (parsed.password) outbound.password = decodeURIComponent(parsed.password);
    if (parsed.protocol === "https:") outbound.tls = { enabled: true };
    return outbound;
}

function parseVless(parsed) {
    const params = parsed.searchParams;
    const outbound = { type: "vless", tag: "proxy", server: parsed.hostname, server_port: parseInt(parsed.port || 443), uuid: decodeURIComponent(parsed.username || "") };
    
    const flow = params.get("flow") || "";
    if (flow) outbound.flow = flow;
    
    const security = params.get("security") || "";
    if (["tls", "reality", "anytls"].includes(security)) {
        const tls = { enabled: true };
        
        const sni = params.get("sni") || "";
        if (sni) tls.server_name = sni;
        
        const alpn = params.get("alpn") || "";
        if (alpn) tls.alpn = alpn.split(",");
        
        const fp = params.get("fp") || "";
        if (fp) tls.utls = { enabled: true, fingerprint: fp };
        
        const insecure = params.get("insecure") || params.get("allowInsecure") || "0";
        if (insecure === "1" || security === "anytls") {
            tls.insecure = true;
        }
        
        if (security === "reality") {
            const reality = { enabled: true };
            const pbk = params.get("pbk") || "";
            if (pbk) reality.public_key = pbk;
            const sid = params.get("sid") || "";
            if (sid) reality.short_id = sid;
            tls.reality = reality;
        }
        outbound.tls = tls;
    }
    
    const netType = params.get("type") || "";
    if (netType === "ws") {
        const transport = { type: "ws" };
        const path = params.get("path") || "";
        if (path) transport.path = decodeURIComponent(path);
        const host = params.get("host") || "";
        if (host) transport.headers = { Host: host };
        outbound.transport = transport;
    }
    return outbound;
}

function parseVmess(urlStr) {
    const encoded = urlStr.substring("vmess://".length);
    const decoded = Buffer.from(encoded, 'base64').toString('utf-8');
    const cfg = JSON.parse(decoded);
    
    const outbound = { 
        type: "vmess", 
        tag: "proxy", 
        server: cfg.add || "", 
        server_port: parseInt(cfg.port || 443), 
        uuid: cfg.id || "", 
        security: cfg.scy || "auto" 
    };
    
    if (["tls", "anytls"].includes(cfg.tls) || cfg.sni) {
        const tls = { enabled: true };
        if (cfg.sni) tls.server_name = cfg.sni;
        else if (cfg.host) tls.server_name = cfg.host;
        
        const alpn = cfg.alpn || "";
        if (alpn) tls.alpn = alpn.split(",");
        
        if (cfg.tls === "anytls") {
            tls.insecure = true;
        }
        outbound.tls = tls;
    }
    
    if (cfg.net === "ws") {
        const transport = { type: "ws" };
        if (cfg.path) transport.path = cfg.path;
        if (cfg.host) transport.headers = { Host: cfg.host };
        outbound.transport = transport;
    }
    return outbound;
}

function parseTrojan(parsed) {
    const params = parsed.searchParams;
    const outbound = { type: "trojan", tag: "proxy", server: parsed.hostname, server_port: parseInt(parsed.port || 443), password: decodeURIComponent(parsed.username || "") };
    
    const security = params.get("security") || "tls";
    if (["tls", "anytls"].includes(security)) {
        const tls = { enabled: true };
        
        const sni = params.get("sni") || "";
        if (sni) tls.server_name = sni;
        
        const alpn = params.get("alpn") || "";
        if (alpn) tls.alpn = alpn.split(",");
        
        const insecure = params.get("insecure") || params.get("allowInsecure") || "0";
        if (insecure === "1" || security === "anytls") {
            tls.insecure = true;
        }
        outbound.tls = tls;
    }
    
    const netType = params.get("type") || "";
    if (netType === "ws") {
        const transport = { type: "ws" };
        const path = params.get("path") || "";
        if (path) transport.path = decodeURIComponent(path);
        const host = params.get("host") || "";
        if (host) transport.headers = { Host: host };
        outbound.transport = transport;
    }
    return outbound;
}

function parseHysteria2(parsed) {
    const params = parsed.searchParams;
    const outbound = { type: "hysteria2", tag: "proxy", server: parsed.hostname, server_port: parseInt(parsed.port || 443), password: decodeURIComponent(parsed.username || "") };
    
    const tls = { enabled: true };
    const sni = params.get("sni") || "";
    if (sni) tls.server_name = sni;
    
    const alpn = params.get("alpn") || "";
    if (alpn) tls.alpn = alpn.split(",");
    
    const insecure = params.get("insecure") || params.get("allowInsecure") || "0";
    if (insecure === "1") tls.insecure = true;
    
    outbound.tls = tls;
    return outbound;
}

function parseTuic(parsed) {
    const params = parsed.searchParams;
    const outbound = { type: "tuic", tag: "proxy", server: parsed.hostname, server_port: parseInt(parsed.port || 443), uuid: "", password: "", congestion_control: params.get("congestion_control") || "bbr" };
    
    const userPart = decodeURIComponent(parsed.username || "");
    const passPart = decodeURIComponent(parsed.password || "");
    
    const colonIdx = userPart.indexOf(":");
    if (colonIdx !== -1 && !passPart) {
        outbound.uuid = userPart.substring(0, colonIdx);
        outbound.password = userPart.substring(colonIdx + 1);
    } else {
        outbound.uuid = userPart;
        outbound.password = passPart;
    }
    
    const tls = { enabled: true };
    const sni = params.get("sni") || "";
    if (sni) tls.server_name = sni;
    
    const alpn = params.get("alpn") || "";
    if (alpn) tls.alpn = alpn.split(",");
    
    const insecure = params.get("insecure") || params.get("allowInsecure") || "0";
    if (insecure === "1") tls.insecure = true;
    
    outbound.tls = tls;
    return outbound;
}

function parseShadowsocks(parsed) {
    const outbound = { type: "shadowsocks", tag: "proxy", server: parsed.hostname, server_port: parseInt(parsed.port || 8388) };
    
    const userPart = decodeURIComponent(parsed.username || "");
    const passPart = decodeURIComponent(parsed.password || "");
    
    const authStr = passPart ? `${userPart}:${passPart}` : userPart;
    let method = userPart;
    let password = passPart;
    
    try {
        const b64Str = authStr.replace(/-/g, "+").replace(/_/g, "/");
        const decoded = Buffer.from(b64Str, 'base64').toString('utf-8');
        const colonIdx = decoded.indexOf(":");
        if (colonIdx !== -1) {
            method = decoded.substring(0, colonIdx);
            password = decoded.substring(colonIdx + 1);
        } else {
            method = userPart;
            password = passPart;
        }
    } catch (err) {
        method = userPart;
        password = passPart;
    }

    outbound.method = method;
    outbound.password = password;
    return outbound;
}

function main() {
    let proxyUrl = (process.env.PROXY_URL || "").trim();
    if (!proxyUrl) {
        console.log("No PROXY_URL provided.");
        process.exit(0);
    }

    if (!proxyUrl.includes("://")) {
        try {
            const b64Str = proxyUrl.replace(/-/g, "+").replace(/_/g, "/");
            const decodedText = Buffer.from(b64Str, 'base64').toString('utf-8').trim();
            
            if (decodedText.includes("://")) {
                const firstValidUrl = decodedText.split(/\r?\n/).find(line => line.includes("://"));
                if (firstValidUrl) {
                    proxyUrl = firstValidUrl.trim();
                }
            }
        } catch (err) {
        }
    }
    
    const scheme = proxyUrl.split("://")[0].toLowerCase();
    let outbound;
    
    try {
        if (scheme === "vmess") {
            outbound = parseVmess(proxyUrl);
        } else {
            let parsed;
            try {
                parsed = new URL(proxyUrl);
            } catch (err) {
                console.error("Invalid URL format:", proxyUrl);
                process.exit(1);
            }
            
            if (scheme === "socks5") outbound = parseSocks5(parsed);
            else if (["http", "https"].includes(scheme)) outbound = parseHttp(parsed);
            else if (scheme === "vless") outbound = parseVless(parsed);
            else if (scheme === "trojan") outbound = parseTrojan(parsed);
            else if (["hy2", "hysteria2"].includes(scheme)) outbound = parseHysteria2(parsed);
            else if (scheme === "tuic") outbound = parseTuic(parsed);
            else if (["ss", "shadowsocks"].includes(scheme)) outbound = parseShadowsocks(parsed);
            else {
                console.error("Unsupported protocol:", scheme);
                process.exit(1);
            }
        }
    } catch (e) {
        console.error("Error parsing URL:", e.message);
        process.exit(1);
    }
            
    const config = {
        log: { level: "fatal", timestamp: true },
        inbounds: [{ type: "mixed", tag: "mixed-in", listen: LISTEN_HOST, listen_port: LISTEN_PORT }],
        outbounds: [outbound, { type: "direct", tag: "direct" }]
    };
    
    fs.writeFileSync("config.json", JSON.stringify(config, null, 2), "utf-8");
    console.log("Generated config.json successfully.");
}

if (require.main === module) {
    main();
}

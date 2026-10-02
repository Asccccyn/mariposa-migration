// CB-005（2026-10-02 审计 P1）离线回归：当前真实 index.html 的
// Source 会话/消息时间 metadata 必须以转义文本进入 innerHTML。
// 方式与审计取证一致：VM 执行页面 <script>，无浏览器/服务/网络。
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const html = fs.readFileSync(
    path.join(__dirname, '..', '..', '..',
              'backend', 'mariposa', 'web', 'index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

const elements = new Map();
function element(id = '') {
    return {id, value: id === 'holdCat' ? 'daily' : '', innerHTML: '',
            style: {}, children: [],
            classList: {add() {}, remove() {}},
            addEventListener() {}, appendChild(e) {this.children.push(e)},
            insertAdjacentHTML(_, s) {this.innerHTML += s},
            remove() {}, scrollIntoView() {}};
}
class FixedDate extends Date {
    static now() { return Date.parse('2026-10-02T12:00:00Z'); }
}
const ctx = vm.createContext({
    console, Date: FixedDate,
    document: {
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, element(id));
            return elements.get(id);
        },
        queryAll() { return []; },
        querySelectorAll() { return []; },
        createElement() { return element(); },
    },
    localStorage: {getItem() {return '';}, setItem() {}},
    prompt() { return 'synthetic'; },
    fetch() { throw Error('AUDIT_NETWORK_FORBIDDEN'); },
    crypto: {randomUUID() {return 'synthetic-id';}},
    setTimeout() {},
});

vm.runInContext(script, ctx, {timeout: 1000});

// 反例载荷：审计同款无害标记。会话 created_at 与消息 created_at 各一处。
const payload = '<b data-audit="metadata">synthetic</b>';
ctx.response = {
    conversation: {title: 't', message_count: 1,
                   created_at: payload, updated_at: payload,
                   provider_conversation_id: 'p'},
    messages: [{provider_message_id: 'm1', normalized_sender: 'human',
                created_at: payload, occurred_date: '',
                text: 'plain', attachments: []}],
    has_more: false,
};
vm.runInContext('renderSourceConv(response,false)', ctx, {timeout: 1000});

const markup = elements.get('srcConvView').innerHTML;
const raw = markup.includes(payload);
const escaped = markup.includes('&lt;b data-audit=');
console.log(JSON.stringify({
    case: 'cb005-source-time-escape',
    raw_markup_in_innerHTML: raw,
    escaped_text_present: escaped,
}));
if (raw || !escaped) process.exitCode = 1;

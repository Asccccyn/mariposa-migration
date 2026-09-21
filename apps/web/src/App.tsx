import { useCallback, useState } from "react";
import { getWho, getToken, saveAuth } from "./api";
import { Calendar, Content, Deletions, Memories, Plans, Quotes, Workspace }
  from "./pages";

const TABS = [
  { id: "mem", label: "记忆 / 检索" },
  { id: "ws", label: "工作区 / 审批" },
  { id: "cal", label: "日历" },
  { id: "plan", label: "计划" },
  { id: "quote", label: "她的话" },
  { id: "del", label: "删除申请" },
  { id: "content", label: "Home / Self / 日记" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function App() {
  const [tab, setTab] = useState<TabId>("mem");
  const [who, setWho] = useState(getWho());
  const [token, setToken] = useState(getToken());
  const [status, setStatus] = useState<{ text: string; err?: boolean } | null>(null);
  // 固定引用：否则子组件 useCallback/useEffect 依赖失效，造成无限请求循环
  const note = useCallback(
    (text: string, err?: boolean) => setStatus({ text, err }), []);

  return (
    <>
      <header>
        <h1>mariposa</h1>
        <div className="who">
          <select value={who} onChange={(e) => setWho(e.target.value)}>
            <option value="qiaosheng">江乔生（qiaosheng）</option>
            <option value="jiaming">周家明（jiaming）</option>
            <option value="worker">工具人（worker）</option>
          </select>
          <input value={token} onChange={(e) => setToken(e.target.value)}
                 placeholder="token" size={24} />
          <button className="primary" onClick={() => {
            saveAuth(who, token);
            note(`已连接为 ${who}`);
            window.location.reload();
          }}>连接</button>
        </div>
        {status ? <span className={status.err ? "status err-text" : "status"}>
          {status.text}</span> : null}
      </header>
      <nav>
        {TABS.map((t) => (
          <button key={t.id} className={tab === t.id ? "active" : ""}
                  onClick={() => setTab(t.id)}>{t.label}</button>
        ))}
      </nav>
      <main>
        {tab === "mem" ? <Memories note={note} /> : null}
        {tab === "ws" ? <Workspace note={note} /> : null}
        {tab === "cal" ? <Calendar /> : null}
        {tab === "plan" ? <Plans note={note} /> : null}
        {tab === "quote" ? <Quotes note={note} /> : null}
        {tab === "del" ? <Deletions note={note} /> : null}
        {tab === "content" ? <Content note={note} /> : null}
      </main>
    </>
  );
}

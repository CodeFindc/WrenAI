import React, { useState, useEffect } from 'react';
import { Sidebar } from './components/Sidebar';
import type { ChatSession } from './components/Sidebar';
import { SettingsModal } from './components/SettingsModal';
import { MessageList } from './components/MessageList';
import { ChatInput } from './components/ChatInput';
import { parseNDJSONStream } from './utils/stream';
import type { Message, StreamUpdate } from './utils/stream';
import { HelpCircle, X } from 'lucide-react';

// Memory storage fallback when window.localStorage throws SecurityError (like in strict browser file:// protocol environments)
const memoryStorage: Record<string, string> = {};
const safeLocalStorage = {
  getItem: (key: string): string | null => {
    try {
      return window.localStorage.getItem(key);
    } catch (e) {
      console.warn(`localStorage.getItem failed for key "${key}", falling back to memory storage:`, e);
      return memoryStorage[key] || null;
    }
  },
  setItem: (key: string, value: string): void => {
    try {
      window.localStorage.setItem(key, value);
    } catch (e) {
      console.warn(`localStorage.setItem failed for key "${key}", falling back to memory storage:`, e);
      memoryStorage[key] = value;
    }
  },
  removeItem: (key: string): void => {
    try {
      window.localStorage.removeItem(key);
    } catch (e) {
      console.warn(`localStorage.removeItem failed for key "${key}", falling back to memory storage:`, e);
      delete memoryStorage[key];
    }
  }
};

// Override localStorage in this file's scope
const localStorage = safeLocalStorage;

// Safe UUID fallback for insecure contexts (like file:// protocol offline) where window.crypto.randomUUID is undefined
const safeCrypto = {
  randomUUID: (): string => {
    try {
      if (typeof window !== 'undefined' && window.crypto && window.crypto.randomUUID) {
        return window.crypto.randomUUID();
      }
    } catch (e) {}
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      const v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }
};

// Override crypto in this file's scope
const crypto = safeCrypto;

// Helper to dynamically resolve the default backend URL based on browser URL's IP + Port
const getDefaultBackendUrl = (): string => {
  if (typeof window !== 'undefined' && window.location) {
    const origin = window.location.origin;
    // If it's a valid HTTP/HTTPS origin (not a file:// protocol), default to it!
    if (origin && !origin.startsWith('file:')) {
      return origin;
    }
  }
  return 'http://localhost:8000';
};

export const App: React.FC = () => {
  // --- States ---
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  
  const [isLoading, setIsLoading] = useState(false);
  const [activeNode, setActiveNode] = useState<string | null>(null);
  
  const [backendUrl, setBackendUrl] = useState(getDefaultBackendUrl());
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [isSupportOpen, setIsSupportOpen] = useState(false);

  // --- Initial Mount Load ---
  useEffect(() => {
    // 1. Load Backend URL
    const savedUrl = localStorage.getItem('wren_backend_url');
    if (savedUrl) {
      setBackendUrl(savedUrl);
    } else {
      const defaultUrl = getDefaultBackendUrl();
      setBackendUrl(defaultUrl);
      localStorage.setItem('wren_backend_url', defaultUrl);
    }

    // 2. Load Sessions
    const savedSessions = localStorage.getItem('wren_recent_chats');
    if (savedSessions) {
      try {
        const parsed: ChatSession[] = JSON.parse(savedSessions);
        setSessions(parsed);
        
        // Load active session ID if exists
        const savedActiveId = localStorage.getItem('wren_active_session_id');
        if (savedActiveId && parsed.some(s => s.id === savedActiveId)) {
          setActiveSessionId(savedActiveId);
          loadSessionMessages(savedActiveId);
        } else if (parsed.length > 0) {
          setActiveSessionId(parsed[0].id);
          loadSessionMessages(parsed[0].id);
        }
      } catch (e) {
        console.error('Error parsing saved sessions:', e);
      }
    } else {
      // Create a default session to make it clean
      handleCreateSession();
    }
  }, []);

  // --- Helper: Save Sessions list to LocalStorage ---
  const saveSessions = (updated: ChatSession[]) => {
    setSessions(updated);
    localStorage.setItem('wren_recent_chats', JSON.stringify(updated));
  };

  // --- Helper: Load Messages for a specific session ---
  const loadSessionMessages = async (sessionId: string) => {
    // Try to load cached messages from localStorage first
    const cached = localStorage.getItem(`wren_messages_${sessionId}`);
    if (cached) {
      try {
        setMessages(JSON.parse(cached));
      } catch {
        setMessages([]);
      }
    } else {
      setMessages([]);
    }

    // Attempt to sync and retrieve full history from server in the background
    try {
      const serverUrl = localStorage.getItem('wren_backend_url') || backendUrl;
      const response = await fetch(`${serverUrl.replace(/\/$/, '')}/chat/history/${sessionId}`);
      if (response.ok) {
        const data = await response.json();
        if (data.messages && Array.isArray(data.messages)) {
          setMessages(data.messages);
          localStorage.setItem(`wren_messages_${sessionId}`, JSON.stringify(data.messages));
        }
      }
    } catch (e) {
      console.warn('Backend history sync unavailable:', e);
    }
  };

  // --- Sidebar Actions ---
  const handleSelectSession = (id: string) => {
    if (isLoading) return; // Prevent switching sessions while loading a response
    setActiveSessionId(id);
    localStorage.setItem('wren_active_session_id', id);
    loadSessionMessages(id);
  };

  const handleCreateSession = () => {
    if (isLoading) return;
    const newId = crypto.randomUUID();
    const newSession: ChatSession = {
      id: newId,
      title: 'New Chat Session',
      timestamp: Date.now(),
    };

    const updated = [newSession, ...sessions];
    saveSessions(updated);
    setActiveSessionId(newId);
    localStorage.setItem('wren_active_session_id', newId);
    setMessages([]);
    localStorage.setItem(`wren_messages_${newId}`, JSON.stringify([]));
  };

  const handleDeleteSession = (id: string) => {
    if (isLoading) return;
    const updated = sessions.filter(s => s.id !== id);
    saveSessions(updated);
    localStorage.removeItem(`wren_messages_${id}`);

    if (activeSessionId === id) {
      if (updated.length > 0) {
        setActiveSessionId(updated[0].id);
        localStorage.setItem('wren_active_session_id', updated[0].id);
        loadSessionMessages(updated[0].id);
      } else {
        setActiveSessionId(null);
        localStorage.removeItem('wren_active_session_id');
        setMessages([]);
      }
    }
  };

  const handleRenameSession = (id: string, newTitle: string) => {
    const updated = sessions.map(s => (s.id === id ? { ...s, title: newTitle } : s));
    saveSessions(updated);
  };

  // --- Settings Actions ---
  const handleSaveSettings = (url: string) => {
    const formattedUrl = url.trim().replace(/\/$/, '');
    setBackendUrl(formattedUrl);
    localStorage.setItem('wren_backend_url', formattedUrl);
  };

  // --- Send Message and Streaming Logic ---
  const handleSendMessage = async (text: string) => {
    if (isLoading || !activeSessionId) return;

    // 1. Create and append User Message
    const userMsg: Message = {
      role: 'user',
      type: 'HumanMessage',
      content: text,
      id: crypto.randomUUID(),
    };

    const updatedMsgs = [...messages, userMsg];
    setMessages(updatedMsgs);
    localStorage.setItem(`wren_messages_${activeSessionId}`, JSON.stringify(updatedMsgs));

    // 2. Automatically rename session if it's the default name
    const currentSession = sessions.find(s => s.id === activeSessionId);
    if (currentSession && currentSession.title === 'New Chat Session') {
      const truncatedTitle = text.length > 25 ? `${text.slice(0, 22)}...` : text;
      handleRenameSession(activeSessionId, truncatedTitle);
    }

    // 3. Initiate Streaming Call to Backend
    setIsLoading(true);
    setActiveNode('agent');

    try {
      const response = await fetch(`${backendUrl.replace(/\/$/, '')}/chat/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          question: text,
          session_id: activeSessionId,
          model_name: 'gpt-4o',
        }),
      });

      if (!response.ok) {
        throw new Error(`Server error: ${response.status} ${response.statusText}`);
      }

      let activeMessagesList = [...updatedMsgs];

      await parseNDJSONStream(
        response,
        (update: StreamUpdate) => {
          // Process Node update
          if (update.agent) {
            setActiveNode('agent');
            const agentMsgs = update.agent.messages || [];
            
            // Merge messages safely to avoid duplicates
            agentMsgs.forEach(msg => {
              const duplicateIndex = activeMessagesList.findIndex(
                existing => 
                  existing.role === msg.role && 
                  existing.content === msg.content && 
                  // If tool calls exist, ensure we match them
                  JSON.stringify(existing.tool_calls) === JSON.stringify(msg.tool_calls)
              );
              
              if (duplicateIndex === -1) {
                activeMessagesList.push(msg);
              }
            });
            
            setMessages([...activeMessagesList]);
            localStorage.setItem(`wren_messages_${activeSessionId}`, JSON.stringify(activeMessagesList));
          }
          
          if (update.tools) {
            setActiveNode('tools');
            const toolMsgs = update.tools.messages || [];
            
            toolMsgs.forEach(msg => {
              const duplicateIndex = activeMessagesList.findIndex(
                existing => 
                  existing.role === msg.role && 
                  existing.tool_call_id === msg.tool_call_id && 
                  existing.tool_call_id !== undefined
              );
              
              if (duplicateIndex === -1) {
                activeMessagesList.push(msg);
              } else {
                activeMessagesList[duplicateIndex] = msg; // Update result
              }
            });
            
            setMessages([...activeMessagesList]);
            localStorage.setItem(`wren_messages_${activeSessionId}`, JSON.stringify(activeMessagesList));
          }
        },
        (errorMsg: string) => {
          // Stream error handler
          console.error('Stream Parsing Error:', errorMsg);
          const errorBubble: Message = {
            role: 'assistant',
            type: 'AIMessage',
            content: `⚠️ Connection Error: ${errorMsg}. Please check if the backend server is running and configured correctly in Settings.`,
            id: crypto.randomUUID(),
          };
          setMessages(prev => {
            const result = [...prev, errorBubble];
            localStorage.setItem(`wren_messages_${activeSessionId}`, JSON.stringify(result));
            return result;
          });
        }
      );
    } catch (err: any) {
      console.error('Fetch Connection Error:', err);
      const networkErrorBubble: Message = {
        role: 'assistant',
        type: 'AIMessage',
        content: `⚠️ Failed to connect to the backend server at \`${backendUrl}\`. Please make sure the server is running (\`python examples/langgraph_fastapi_multi.py\`) and verify your connection in Settings.`,
        id: crypto.randomUUID(),
      };
      setMessages(prev => {
        const result = [...prev, networkErrorBubble];
        localStorage.setItem(`wren_messages_${activeSessionId}`, JSON.stringify(result));
        return result;
      });
    } finally {
      setIsLoading(false);
      setActiveNode(null);
    }
  };

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-white">
      {/* Sidebar Panel */}
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={handleSelectSession}
        onCreateSession={handleCreateSession}
        onDeleteSession={handleDeleteSession}
        onRenameSession={handleRenameSession}
        onOpenSettings={() => setIsSettingsOpen(true)}
        onOpenSupport={() => setIsSupportOpen(true)}
      />

      {/* Main Chat Panel */}
      <div className="flex-1 flex flex-col h-full overflow-hidden relative bg-slate-50/10">
        {/* Top Header */}
        <div className="h-16 border-b border-slate-100 px-6 flex items-center justify-between bg-white/80 backdrop-blur-md select-none shrink-0 z-10">
          <div className="flex items-center space-x-2">
            <span className="text-sm font-semibold text-slate-800">
              {sessions.find(s => s.id === activeSessionId)?.title || 'Wren Chat'}
            </span>
          </div>
        </div>

        {/* Message Panel */}
        <MessageList
          messages={messages}
          isLoading={isLoading}
          activeNode={activeNode}
        />

        {/* Input Form Panel */}
        <ChatInput onSendMessage={handleSendMessage} isLoading={isLoading} />
      </div>

      {/* Settings Modal */}
      <SettingsModal
        isOpen={isSettingsOpen}
        onClose={() => setIsSettingsOpen(false)}
        backendUrl={backendUrl}
        onSave={handleSaveSettings}
      />

      {/* Support Instructions Modal */}
      {isSupportOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-sm animate-fade-in select-none">
          <div className="relative w-full max-w-lg overflow-hidden bg-white rounded-2xl shadow-2xl border border-slate-100 flex flex-col animate-scale-up">
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
              <h3 className="text-lg font-semibold text-slate-900 flex items-center">
                <HelpCircle className="mr-2 text-blue-600" size={20} /> System Support & Running Instructions
              </h3>
              <button
                onClick={() => setIsSupportOpen(false)}
                className="p-1 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition-colors cursor-pointer"
              >
                <X size={20} />
              </button>
            </div>
            
            <div className="p-6 space-y-4 text-slate-600 overflow-y-auto max-h-[70svh]">
              <p className="text-sm leading-relaxed">
                This Single Page Application (SPA) is designed to run completely **offline** (no internet CDN dependencies). It interfaces directly with the stateful, multi-turn LangGraph server located in your WrenAI project SDK.
              </p>

              <div className="space-y-2">
                <h4 className="text-xs font-bold text-slate-400 uppercase tracking-wider">How to Run Backend Server</h4>
                <p className="text-xs text-slate-500">
                  Open your terminal in the WrenAI repository and execute:
                </p>
                <pre className="bg-slate-900 text-slate-300 p-3.5 rounded-xl font-mono text-xs overflow-x-auto select-text leading-4">
                  <code>python examples/langgraph_fastapi_multi.py</code>
                </pre>
              </div>

              <div className="space-y-2">
                <h4 className="text-xs font-bold text-slate-400 uppercase tracking-wider">Configuring Variables</h4>
                <p className="text-xs text-slate-500">
                  Make sure you have set the required environment variables:
                </p>
                <pre className="bg-slate-900 text-slate-300 p-3.5 rounded-xl font-mono text-xs overflow-x-auto select-text leading-4">
                  <code># Set OpenAI credentials<br />
export OPENAI_API_KEY=sk-...<br />
# Path to your Wren Project directory<br />
export PROJECT_PATH=/path/to/your-wren-project</code>
                </pre>
              </div>

              <div className="space-y-2">
                <h4 className="text-xs font-bold text-slate-400 uppercase tracking-wider">Testing Connectivity</h4>
                <p className="text-sm">
                  Go to <strong className="text-slate-800">Settings</strong> in the sidebar, input the API URL (defaults to <code className="bg-slate-100 text-slate-700 px-1 py-0.5 rounded text-xs">http://localhost:8000</code>), and click <strong className="text-slate-800">Test Connection</strong> to verify live integration!
                </p>
              </div>
            </div>

            <div className="px-6 py-4 bg-slate-50 border-t border-slate-100 flex justify-end">
              <button
                onClick={() => setIsSupportOpen(false)}
                className="px-5 py-2.5 bg-blue-600 hover:bg-blue-700 text-white font-medium rounded-xl text-sm shadow-md shadow-blue-500/10 cursor-pointer transition-colors"
              >
                Got it, thanks!
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

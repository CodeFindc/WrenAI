import React, { useState, useEffect, useRef } from 'react';
import { Database, Terminal, ChevronDown, ChevronUp, Copy, Check, Sparkles, User } from 'lucide-react';
import type { Message, ToolCall } from '../utils/stream';

interface MessageListProps {
  messages: Message[];
  isLoading: boolean;
  activeNode: string | null;
  toolProgress: {
    server_name: string;
    tool_name: string;
    progress: number;
    total: number;
    message: string;
  } | null;
}

// Simple local Markdown parser to avoid external package loading issues offline
const renderMarkdownContent = (text: string) => {
  if (!text) return '';
  
  // Format code blocks
  let formatted = text;

  // Split content by code blocks
  const parts = formatted.split(/(```[\s\S]*?```)/g);
  return parts.map((part, index) => {
    if (part.startsWith('```')) {
      const match = part.match(/```(\w*)\n([\s\S]*?)```/);
      const lang = match ? match[1] : 'text';
      const code = match ? match[2] : part.slice(3, -3);
      return (
        <CodeBlock key={index} language={lang} code={code.trim()} />
      );
    }

    // Process inline formatting (bold, lists, inline code)
    const subParts = part.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
    const inlineContent = subParts.map((subPart, subIndex) => {
      if (subPart.startsWith('`') && subPart.endsWith('`')) {
        return (
          <code key={subIndex} className="bg-slate-100 text-rose-600 px-1.5 py-0.5 rounded font-mono text-sm border border-slate-200">
            {subPart.slice(1, -1)}
          </code>
        );
      }
      if (subPart.startsWith('**') && subPart.endsWith('**')) {
        return (
          <strong key={subIndex} className="font-bold text-slate-900">
            {subPart.slice(2, -2)}
          </strong>
        );
      }
      return subPart;
    });

    // Format newlines into paragraph breaks or bullet points
    return (
      <div key={index} className="space-y-2 whitespace-pre-wrap leading-relaxed text-sm md:text-base text-slate-700">
        {inlineContent}
      </div>
    );
  });
};

// Bulletproof copy helper supporting both secure contexts (navigator.clipboard) and insecure fallback (execCommand)
const copyTextToClipboard = async (text: string): Promise<boolean> => {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      console.warn("navigator.clipboard.writeText failed, trying fallback:", err);
    }
  }

  // Fallback for insecure context (HTTP IP addresses, file:// protocol)
  try {
    const textArea = document.createElement("textarea");
    textArea.value = text;
    textArea.style.position = "fixed";
    textArea.style.top = "0";
    textArea.style.left = "0";
    textArea.style.width = "2em";
    textArea.style.height = "2em";
    textArea.style.padding = "0";
    textArea.style.border = "none";
    textArea.style.outline = "none";
    textArea.style.boxShadow = "none";
    textArea.style.background = "transparent";
    
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    
    const successful = document.execCommand("copy");
    document.body.removeChild(textArea);
    return successful;
  } catch (err) {
    console.error("Fallback clipboard copy failed:", err);
    return false;
  }
};

const CodeBlock: React.FC<{ language: string; code: string }> = ({ language, code }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    const success = await copyTextToClipboard(code);
    if (success) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <div className="my-4 bg-slate-950 rounded-xl overflow-hidden shadow-md border border-slate-800 text-left font-mono text-sm max-w-full">
      <div className="flex items-center justify-between px-4 py-2 bg-slate-900 text-slate-400 text-xs select-none border-b border-slate-800">
        <span className="uppercase tracking-wider font-semibold text-[10px]">{language || 'code'}</span>
        <button
          onClick={handleCopy}
          className="flex items-center space-x-1 hover:text-white transition-colors cursor-pointer"
        >
          {copied ? <Check size={12} className="text-emerald-500" /> : <Copy size={12} />}
          <span>{copied ? 'Copied' : 'Copy'}</span>
        </button>
      </div>
      <pre className="p-4 overflow-x-auto text-slate-300 select-text leading-5">
        <code>{code}</code>
      </pre>
    </div>
  );
};

const CopyMessageButton: React.FC<{ content: string }> = ({ content }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    const success = await copyTextToClipboard(content);
    if (success) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <button
      onClick={handleCopy}
      className="mt-1.5 text-slate-400 hover:text-slate-600 transition-colors duration-150 flex items-center space-x-1 text-xs font-semibold px-2 py-1 rounded-md hover:bg-slate-100/80 cursor-pointer select-none border border-slate-100 hover:border-slate-200 bg-white/50 backdrop-blur-sm self-start shadow-sm active:scale-[0.98]"
      title="Copy entire response"
    >
      {copied ? (
        <>
          <Check size={12} className="text-emerald-500" />
          <span className="text-emerald-600 text-[10px]">Copied!</span>
        </>
      ) : (
        <>
          <Copy size={12} />
          <span className="text-[10px]">Copy Content</span>
        </>
      )}
    </button>
  );
};

const ToolCallCard: React.FC<{ toolCall: ToolCall; toolResult?: Message }> = ({
  toolCall,
  toolResult,
}) => {
  const [isOpen, setIsOpen] = useState(false);

  const formatArgs = (args: Record<string, any>) => {
    try {
      return JSON.stringify(args, null, 2);
    } catch {
      return '';
    }
  };

  return (
    <div className="my-3 border border-slate-200 rounded-xl bg-white shadow-sm overflow-hidden text-left w-full max-w-full">
      {/* Accordion Trigger */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between px-4 py-3 bg-slate-50 hover:bg-slate-100/80 transition-colors"
      >
        <div className="flex items-center space-x-2.5">
          <div className="p-1.5 bg-blue-50 rounded-lg text-blue-600">
            <Database size={16} />
          </div>
          <div className="flex flex-col items-start min-w-0">
            <span className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider">
              Wren Toolkit
            </span>
            <span className="text-sm font-medium text-slate-700 truncate max-w-[180px] sm:max-w-xs md:max-w-md">
              Executed: <code className="text-blue-600 text-xs px-1 py-0.5 bg-blue-50/50 rounded font-mono font-bold">{toolCall.name}</code>
            </span>
          </div>
        </div>
        <div className="flex items-center space-x-2 shrink-0">
          {toolResult ? (
            <span className="text-[10px] font-bold bg-emerald-50 text-emerald-700 px-2 py-0.5 border border-emerald-100 rounded-full select-none">
              Success
            </span>
          ) : (
            <span className="text-[10px] font-bold bg-amber-50 text-amber-700 px-2 py-0.5 border border-amber-100 rounded-full animate-pulse-slow select-none">
              Running
            </span>
          )}
          {isOpen ? <ChevronUp size={16} className="text-slate-400" /> : <ChevronDown size={16} className="text-slate-400" />}
        </div>
      </button>

      {/* Accordion Content */}
      {isOpen && (
        <div className="p-4 border-t border-slate-100 bg-slate-50/30 text-xs space-y-3 w-full overflow-hidden">
          {/* Tool Arguments */}
          <div className="w-full overflow-hidden">
            <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1 flex items-center">
              <Terminal size={10} className="mr-1 text-slate-400" /> Arguments
            </div>
            <pre className="bg-slate-900 text-slate-300 p-3 rounded-lg font-mono whitespace-pre-wrap break-all select-text leading-4 max-h-40 overflow-y-auto w-full">
              <code>{formatArgs(toolCall.args)}</code>
            </pre>
          </div>

          {/* Tool Result */}
          {toolResult && (
            <div className="w-full overflow-hidden">
              <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">
                Output Results
              </div>
              <pre className="bg-slate-950 text-emerald-400 p-3 rounded-lg font-mono whitespace-pre-wrap break-all select-text max-h-60 overflow-y-auto leading-5 w-full">
                <code>{toolResult.content}</code>
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export const MessageList: React.FC<MessageListProps> = ({
  messages,
  isLoading,
  activeNode,
  toolProgress,
}) => {
  const containerEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    containerEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isLoading]);

  // Group tool messages by their corresponding tool_call_id
  const toolResultsMap = new Map<string, Message>();
  messages.forEach((msg) => {
    if (msg.role === 'tool' && msg.tool_call_id) {
      toolResultsMap.set(msg.tool_call_id, msg);
    }
  });

  return (
    <div className="flex-1 overflow-y-auto px-4 md:px-8 py-6 space-y-6 flex flex-col items-center">
      <div className="w-full max-w-4xl space-y-6">
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-center space-y-4 max-w-md mx-auto">
            <div className="p-4 bg-blue-50 text-blue-600 rounded-2xl shadow-inner animate-pulse-slow">
              <Sparkles size={36} />
            </div>
            <h2 className="text-xl font-bold text-slate-800 leading-tight">
              Question Number With AI Analytics Assistant
            </h2>
            <p className="text-sm text-slate-400 leading-relaxed">
              Ask database analytical questions (e.g. "What data models are loaded?" or "Give me the statistics of suicide events"). The agent will automatically call the semantic toolkit to retrieve schemas and write optimized queries.
            </p>
          </div>
        ) : (
          messages.map((message, index) => {
            // If it's a ToolMessage, it's rendered inside the ToolCall card of the AIMessage.
            // So we skip rendering ToolMessage at the root level.
            if (message.role === 'tool') return null;
            if (message.role === 'system') return null;

            const isUser = message.role === 'user';

            return (
              <div
                key={message.id || index}
                className={`flex w-full space-x-3 md:space-x-4 animate-fade-in ${
                  isUser ? 'justify-end' : 'justify-start'
                }`}
              >
                {/* Assistant Avatar */}
                {!isUser && (
                  <div className="shrink-0 w-9 h-9 bg-blue-600 hover:bg-blue-700 transition-colors flex items-center justify-center rounded-full text-white shadow-md shadow-blue-500/10 self-start">
                    <Sparkles size={16} className="animate-pulse-slow" />
                  </div>
                )}

                {/* Message Bubble Container */}
                <div className={`flex flex-col max-w-[85%] min-w-0 ${isUser ? 'items-end' : 'items-start'}`}>
                  {/* Bubble Metadata */}
                  <span className="text-[11px] font-semibold text-slate-400 mb-1.5 select-none leading-none">
                    {isUser ? 'You • Just now' : 'Nova AI • Just now'}
                  </span>

                  {/* Message Bubble */}
                  <div
                    className={`px-5 py-3.5 shadow-sm w-full max-w-full overflow-hidden ${
                      isUser
                        ? 'bg-blue-600 text-white rounded-2xl rounded-tr-none'
                        : 'bg-white border border-slate-100 text-slate-800 rounded-2xl rounded-tl-none'
                    }`}
                  >
                    <div className="select-text">
                      {isUser ? (
                        <p className="whitespace-pre-wrap leading-relaxed text-sm md:text-base font-medium">
                          {message.content}
                        </p>
                      ) : (
                        renderMarkdownContent(message.content)
                      )}
                    </div>

                    {/* Tool Calls if any exist inside assistant message */}
                    {!isUser && message.tool_calls && message.tool_calls.length > 0 && (
                      <div className="mt-3 space-y-2 select-none">
                        {message.tool_calls.map((toolCall) => {
                          const result = toolResultsMap.get(toolCall.id);
                          return (
                            <ToolCallCard
                              key={toolCall.id}
                              toolCall={toolCall}
                              toolResult={result}
                            />
                          );
                        })}
                      </div>
                    )}
                  </div>
                  {!isUser && <CopyMessageButton content={message.content} />}
                </div>

                {/* User Avatar */}
                {isUser && (
                  <div className="shrink-0 w-9 h-9 bg-slate-200 flex items-center justify-center rounded-full text-slate-600 self-start">
                    <User size={16} />
                  </div>
                )}
              </div>
            );
          })
        )}

        {/* Loading Indicator */}
        {isLoading && (
          <div className="flex w-full space-x-4 justify-start animate-fade-in">
            <div className="shrink-0 w-9 h-9 bg-blue-600 flex items-center justify-center rounded-full text-white shadow-md shadow-blue-500/10 self-start">
              <Sparkles size={16} className="animate-spin text-white" />
            </div>
            <div className="flex flex-col items-start max-w-[80%]">
              <span className="text-[11px] font-semibold text-slate-400 mb-1.5 select-none leading-none">
                Nova AI • {activeNode ? `Executing ${activeNode} Node` : 'Thinking'}
              </span>
              <div className="px-5 py-4 bg-white border border-slate-100 text-slate-500 rounded-2xl rounded-tl-none shadow-sm flex items-center space-x-2">
                <span className="w-1.5 h-1.5 bg-blue-600 rounded-full animate-bounce shrink-0" style={{ animationDelay: '0ms' }} />
                <span className="w-1.5 h-1.5 bg-blue-600 rounded-full animate-bounce shrink-0" style={{ animationDelay: '150ms' }} />
                <span className="w-1.5 h-1.5 bg-blue-600 rounded-full animate-bounce shrink-0" style={{ animationDelay: '300ms' }} />
                <span className="text-xs ml-2 font-medium">
                  {toolProgress ? (
                    <span className="text-slate-700">
                      [{toolProgress.server_name}] {toolProgress.message}{' '}
                      <span className="text-blue-600 font-semibold">
                        ({toolProgress.progress}/{toolProgress.total})
                      </span>
                    </span>
                  ) : activeNode === 'tools' ? (
                    'Retrieving Wren Toolkit database data...'
                  ) : activeNode === 'agent' ? (
                    'Formulating assistant answer...'
                  ) : (
                    'Analyzing query...'
                  )}
                </span>
              </div>
            </div>
          </div>
        )}

        <div ref={containerEndRef} />
      </div>
    </div>
  );
};

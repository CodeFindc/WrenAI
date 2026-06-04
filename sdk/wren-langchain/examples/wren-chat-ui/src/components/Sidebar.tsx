import React, { useState } from 'react';
import { Plus, MessageSquare, Settings, HelpCircle, Trash2, Edit2, Check, X } from 'lucide-react';

export interface ChatSession {
  id: string;
  title: string;
  timestamp: number;
}

interface SidebarProps {
  sessions: ChatSession[];
  activeSessionId: string | null;
  onSelectSession: (id: string) => void;
  onCreateSession: () => void;
  onDeleteSession: (id: string) => void;
  onRenameSession: (id: string, newTitle: string) => void;
  onOpenSettings: () => void;
  onOpenSupport: () => void;
}

export const Sidebar: React.FC<SidebarProps> = ({
  sessions,
  activeSessionId,
  onSelectSession,
  onCreateSession,
  onDeleteSession,
  onRenameSession,
  onOpenSettings,
  onOpenSupport,
}) => {
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState('');

  const handleStartRename = (e: React.MouseEvent, session: ChatSession) => {
    e.stopPropagation();
    setEditingSessionId(session.id);
    setEditTitle(session.title);
  };

  const handleSaveRename = (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    if (editTitle.trim()) {
      onRenameSession(id, editTitle.trim());
    }
    setEditingSessionId(null);
  };

  const handleCancelRename = (e: React.MouseEvent) => {
    e.stopPropagation();
    setEditingSessionId(null);
  };

  return (
    <div className="w-72 bg-slate-50 border-r border-slate-200 flex flex-col h-full shrink-0 select-none">
      {/* Top Header */}
      <div className="px-6 py-7 flex flex-col">
        <h1 className="text-2xl font-bold text-slate-900 tracking-tight leading-none">
          问数智能体
        </h1>
        <span className="text-xs text-slate-400 font-medium tracking-wide mt-1.5 uppercase">
          Powerful For Models
        </span>
      </div>

      {/* New Chat Button */}
      <div className="px-4 mb-4">
        <button
          onClick={onCreateSession}
          className="w-full py-3 px-4 bg-blue-600 hover:bg-blue-700 text-white font-medium rounded-xl flex items-center justify-center space-x-2 transition-all duration-200 active:scale-[0.98] shadow-md shadow-blue-500/10 cursor-pointer"
        >
          <Plus size={18} strokeWidth={2.5} />
          <span>New Chat</span>
        </button>
      </div>

      {/* Recent Chats Section */}
      <div className="flex-1 overflow-y-auto px-2 space-y-0.5">
        <div className="px-3 mb-2 text-xs font-semibold text-slate-400 uppercase tracking-wider">
          Recent Chats
        </div>

        {sessions.length === 0 ? (
          <div className="px-3 py-4 text-sm text-slate-400 italic">
            No recent sessions
          </div>
        ) : (
          sessions.map((session) => {
            const isActive = session.id === activeSessionId;
            const isEditing = session.id === editingSessionId;

            return (
              <div
                key={session.id}
                onClick={() => !isEditing && onSelectSession(session.id)}
                className={`group relative flex items-center px-3 py-3 rounded-xl cursor-pointer text-sm font-medium transition-all duration-150 ${
                  isActive
                    ? 'bg-blue-50/70 text-blue-600'
                    : 'text-slate-600 hover:bg-slate-100 hover:text-slate-950'
                }`}
              >
                <MessageSquare
                  size={16}
                  className={`shrink-0 mr-3 ${isActive ? 'text-blue-500' : 'text-slate-400 group-hover:text-slate-500'}`}
                />

                {isEditing ? (
                  <div className="flex items-center flex-1 space-x-1 pr-6" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="text"
                      value={editTitle}
                      onChange={(e) => setEditTitle(e.target.value)}
                      className="flex-1 min-w-0 bg-white border border-slate-300 rounded px-1.5 py-0.5 text-xs text-slate-800 outline-none focus:border-blue-500"
                      autoFocus
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') handleSaveRename(e as any, session.id);
                        if (e.key === 'Escape') handleCancelRename(e as any);
                      }}
                    />
                    <button
                      onClick={(e) => handleSaveRename(e, session.id)}
                      className="p-0.5 hover:bg-slate-200 rounded text-emerald-600"
                    >
                      <Check size={12} />
                    </button>
                    <button
                      onClick={handleCancelRename}
                      className="p-0.5 hover:bg-slate-200 rounded text-rose-600"
                    >
                      <X size={12} />
                    </button>
                  </div>
                ) : (
                  <span className="truncate pr-12 leading-normal">
                    {session.title}
                  </span>
                )}

                {/* Hover Operations for edit/delete */}
                {!isEditing && (
                  <div className="absolute right-2 opacity-0 group-hover:opacity-100 flex items-center space-x-1 bg-gradient-to-l from-slate-50 group-hover:from-slate-100 pl-4 py-1 rounded-r-xl transition-all duration-150">
                    <button
                      onClick={(e) => handleStartRename(e, session)}
                      className="p-1 rounded text-slate-400 hover:text-slate-600 hover:bg-slate-200/50"
                      title="Rename Session"
                    >
                      <Edit2 size={13} />
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        onDeleteSession(session.id);
                      }}
                      className="p-1 rounded text-slate-400 hover:text-rose-600 hover:bg-rose-50"
                      title="Delete Session"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      {/* Bottom Sidebar Controls */}
      <div className="p-3 border-t border-slate-200 bg-slate-50/50 space-y-1">
        <button
          onClick={onOpenSettings}
          className="w-full flex items-center px-3 py-2.5 rounded-xl text-sm font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900 transition-colors"
        >
          <Settings size={18} className="text-slate-400 mr-3 shrink-0" />
          <span>Settings</span>
        </button>
        <button
          onClick={onOpenSupport}
          className="w-full flex items-center px-3 py-2.5 rounded-xl text-sm font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900 transition-colors"
        >
          <HelpCircle size={18} className="text-slate-400 mr-3 shrink-0" />
          <span>Support</span>
        </button>
      </div>
    </div>
  );
};

import React, { useState } from 'react';
import type { FormEvent, KeyboardEvent } from 'react';
import { ArrowUp, Mic, MicOff } from 'lucide-react';

interface ChatInputProps {
  onSendMessage: (message: string) => void;
  isLoading: boolean;
}

export const ChatInput: React.FC<ChatInputProps> = ({ onSendMessage, isLoading }) => {
  const [text, setText] = useState('');
  const [isRecording, setIsRecording] = useState(false);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (text.trim() && !isLoading) {
      onSendMessage(text.trim());
      setText('');
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const handleMicClick = () => {
    // Demonstration toggle for voice capability
    setIsRecording(!isRecording);
    if (!isRecording) {
      setText('统计自杀事件数量');
      setTimeout(() => {
        setIsRecording(false);
      }, 1500);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="relative w-full max-w-4xl mx-auto px-4 pb-6"
    >
      <div className="flex items-center bg-white border border-slate-100 rounded-[28px] pl-6 pr-2.5 py-2.5 shadow-xl shadow-slate-100 hover:shadow-2xl hover:shadow-slate-200/80 transition-all duration-300 focus-within:ring-2 focus-within:ring-blue-500/10 focus-within:border-blue-500">
        <textarea
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask a question about your database models..."
          disabled={isLoading}
          className="flex-1 bg-transparent text-slate-800 text-sm md:text-base outline-none resize-none max-h-32 py-1 placeholder-slate-400 disabled:opacity-50"
        />

        <div className="flex items-center space-x-2 shrink-0 ml-4">
          {/* Micro icon */}
          <button
            type="button"
            onClick={handleMicClick}
            disabled={isLoading}
            className={`p-2 rounded-full transition-all duration-200 ${
              isRecording
                ? 'bg-rose-50 text-rose-600 animate-pulse'
                : 'text-slate-400 hover:text-slate-600 hover:bg-slate-50'
            }`}
            title="Voice input (Demo)"
          >
            {isRecording ? <MicOff size={20} /> : <Mic size={20} />}
          </button>

          {/* Send button */}
          <button
            type="submit"
            disabled={!text.trim() || isLoading}
            className={`p-3 rounded-full flex items-center justify-center transition-all duration-200 cursor-pointer ${
              text.trim() && !isLoading
                ? 'bg-blue-600 text-white shadow-md shadow-blue-500/20 hover:bg-blue-700 active:scale-95'
                : 'bg-slate-100 text-slate-300 cursor-not-allowed'
            }`}
            title="Send Message"
          >
            <ArrowUp size={18} strokeWidth={2.5} />
          </button>
        </div>
      </div>
    </form>
  );
};

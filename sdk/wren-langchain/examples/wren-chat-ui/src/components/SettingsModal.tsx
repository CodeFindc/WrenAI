import React, { useState, useEffect } from 'react';
import { X, Wifi, AlertCircle, CheckCircle2, Loader2 } from 'lucide-react';

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  backendUrl: string;
  onSave: (url: string) => void;
}

export const SettingsModal: React.FC<SettingsModalProps> = ({
  isOpen,
  onClose,
  backendUrl,
  onSave,
}) => {
  const [url, setUrl] = useState(backendUrl);
  const [testing, setTesting] = useState(false);
  const [testStatus, setTestStatus] = useState<'idle' | 'success' | 'error'>('idle');
  const [testMessage, setTestMessage] = useState('');

  useEffect(() => {
    setUrl(backendUrl);
  }, [backendUrl, isOpen]);

  if (!isOpen) return null;

  const handleTestConnection = async () => {
    setTesting(true);
    setTestStatus('idle');
    setTestMessage('');
    try {
      const controller = new AbortController();
      const id = setTimeout(() => controller.abort(), 5000); // 5s timeout

      const response = await fetch(`${url.replace(/\/$/, '')}/health`, {
        method: 'GET',
        signal: controller.signal,
      });
      clearTimeout(id);

      if (response.ok) {
        const data = await response.json();
        setTestStatus('success');
        setTestMessage(
          `Successfully connected! (Project Loaded: ${
            data.project_loaded ? 'Yes' : 'No'
          }, Memory: ${data.memory_enabled ? 'Enabled' : 'Disabled'})`
        );
      } else {
        setTestStatus('error');
        setTestMessage(`Server returned error: ${response.status} ${response.statusText}`);
      }
    } catch (err: any) {
      setTestStatus('error');
      setTestMessage(
        'Failed to connect. Please verify the URL/port and ensure the backend is running with CORS enabled.'
      );
    } finally {
      setTesting(false);
    }
  };

  const handleSave = () => {
    onSave(url);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-sm animate-fade-in">
      <div className="relative w-full max-w-md overflow-hidden bg-white rounded-2xl shadow-2xl border border-slate-100 flex flex-col animate-scale-up">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <h3 className="text-lg font-semibold text-slate-900">System Settings</h3>
          <button
            onClick={onClose}
            className="p-1 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition-colors"
          >
            <X size={20} />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-4 flex-1">
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">
              Backend Server URL
            </label>
            <div className="relative">
              <input
                type="text"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="e.g. http://localhost:8000"
                className="w-full px-4 py-2 border border-slate-200 rounded-xl text-slate-800 bg-slate-50 hover:bg-white focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all"
              />
            </div>
            <p className="mt-1.5 text-xs text-slate-400">
              The API endpoint where the LangGraph backend is hosted (e.g. `langgraph_fastapi_multi.py`).
            </p>
          </div>

          {/* Test Status Panel */}
          {testStatus !== 'idle' && (
            <div
              className={`p-3 rounded-xl flex items-start space-x-2 border text-sm transition-all ${
                testStatus === 'success'
                  ? 'bg-emerald-50 border-emerald-100 text-emerald-800'
                  : 'bg-rose-50 border-rose-100 text-rose-800'
              }`}
            >
              {testStatus === 'success' ? (
                <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-emerald-600" />
              ) : (
                <AlertCircle size={16} className="mt-0.5 shrink-0 text-rose-600" />
              )}
              <span className="leading-tight">{testMessage}</span>
            </div>
          )}

          {/* Action to test connection */}
          <button
            type="button"
            onClick={handleTestConnection}
            disabled={testing || !url}
            className="w-full py-2.5 px-4 bg-slate-100 hover:bg-slate-200 text-slate-700 font-medium rounded-xl flex items-center justify-center space-x-2 text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-all"
          >
            {testing ? (
              <Loader2 size={16} className="animate-spin text-slate-500" />
            ) : (
              <Wifi size={16} className="text-slate-500" />
            )}
            <span>{testing ? 'Testing Connection...' : 'Test Connection'}</span>
          </button>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end space-x-2 px-6 py-4 bg-slate-50 border-t border-slate-100">
          <button
            onClick={onClose}
            className="px-4 py-2 border border-slate-200 text-slate-600 hover:bg-white hover:text-slate-800 font-medium rounded-xl text-sm transition-all"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={!url}
            className="px-5 py-2 bg-blue-600 hover:bg-blue-700 text-white font-medium rounded-xl text-sm shadow-md shadow-blue-500/10 disabled:opacity-50 transition-all"
          >
            Save Changes
          </button>
        </div>
      </div>
    </div>
  );
};

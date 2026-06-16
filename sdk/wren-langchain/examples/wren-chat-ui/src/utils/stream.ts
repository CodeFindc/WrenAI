export interface ToolCall {
  name: string;
  args: Record<string, any>;
  id: string;
  type?: string;
}

export interface Message {
  role: 'user' | 'assistant' | 'system' | 'tool';
  type: 'HumanMessage' | 'AIMessage' | 'SystemMessage' | 'ToolMessage';
  content: string;
  tool_calls?: ToolCall[];
  name?: string;
  tool_call_id?: string;
  id?: string; // local client id to help React rendering
}

export interface StreamUpdate {
  session_id: string;
  agent?: {
    messages: Message[];
  };
  tools?: {
    messages: Message[];
  };
  progress?: {
    server_name: string;
    tool_name: string;
    progress: number;
    total: number;
    message: string;
  };
  error?: string;
}

/**
 * Parses an NDJSON stream from the FastAPI chat stream endpoint.
 * @param response The Fetch response object containing the stream body.
 * @param onChunk Callback triggered when a new JSON chunk is successfully parsed.
 * @param onError Callback triggered when an error occurs during parsing or in the stream.
 */
export async function parseNDJSONStream(
  response: Response,
  onChunk: (update: StreamUpdate) => void,
  onError: (error: string) => void
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) {
    onError("No readable stream in response body");
    return;
  }

  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      // Keep the last, potentially incomplete line in the buffer
      buffer = lines.pop() || "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;

        try {
          const parsed: StreamUpdate = JSON.parse(trimmed);
          if (parsed.error) {
            onError(parsed.error);
          } else {
            onChunk(parsed);
          }
        } catch (e) {
          console.error("Error parsing NDJSON chunk line:", trimmed, e);
        }
      }
    }

    // Process any remaining content in the buffer
    if (buffer.trim()) {
      try {
        const parsed: StreamUpdate = JSON.parse(buffer.trim());
        if (parsed.error) {
          onError(parsed.error);
        } else {
          onChunk(parsed);
        }
      } catch (e) {
        console.error("Error parsing final NDJSON buffer chunk:", buffer, e);
      }
    }
  } catch (err: any) {
    onError(err?.message || "Stream reading error");
  } finally {
    reader.releaseLock();
  }
}

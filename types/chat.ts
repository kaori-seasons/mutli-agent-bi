export interface Message {
  role: Role;
  content: string;
  sql?: string;
  chartData?: any[];
  extra?: string,
  chartType?: string;
}

export type Role = 'assistant' | 'user';

export interface ChatBody {
  messages: Message[];
  userId: string;
  modelType: string | undefined;
  modelId: string | undefined;
}

export interface Conversation {
  id: string;
  name: string;
  messages: Message[];
  model: string;
  prompt?: string;
  temperature: number;
  folderId: string | null;
}

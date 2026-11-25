export interface Message {
  role: Role;
  content: string;
  sql?: string;
  chartData?: any[];
  extra?: string,
  chartType?: string;
  ontology?: {
    release_id: string;
    template_id: string;
    template_version: number;
    dependency_versions: Record<string, number>;
    sql_sha256: string;
    events: string[];
  };
}

export type Role = 'assistant' | 'user';

export interface ChatBody {
  messages: Message[];
  userId: string; // Legacy client field; never used as server identity.
  sourceIds?: string[];
  ontology?: { release_id?: string; template_id?: string; parameters?: unknown[] };
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

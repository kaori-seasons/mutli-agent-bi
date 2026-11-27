import type { NextApiRequest, NextApiResponse } from 'next';
import { backendRequest, requireAdmin, requireBearer } from './backend';

export async function ontologyProxy(req: NextApiRequest, res: NextApiResponse, area: 'ontology' | 'knowledge') {
  const parts = req.query.path;
  if (!Array.isArray(parts) || parts.some(p => typeof p !== 'string')) return res.status(404).end();
  const path = parts.join('/');
  const method = req.method;
  const id = '[0-9a-f]{32}';
  const adminRoutes: Record<string, RegExp> = {
    GET: new RegExp(`^(releases|audit|changesets|changesets/${id}|discovery/config|discovery/runs|discovery/runs/${id})$`),
    POST: new RegExp(`^(migrate-v1|changesets|changesets/${id}/review|releases|activate|releases/${id}/quarantine|discovery/runs|discovery/runs/${id}/prepare-review)$`),
  };
  const readRoutes: Record<string, RegExp> = { GET: /^entities\/[^/]{1,160}$/, POST: /^search$/ };
  if (!(area === 'ontology' ? adminRoutes : readRoutes)[method || '']?.test(path)) return res.status(404).end();
  if (area === 'ontology' ? !(await requireAdmin(req, res)) : !requireBearer(req, res)) return;
  const release = req.query.release_id;
  if (release !== undefined && (typeof release !== 'string' || !/^[0-9a-f]{32}$/.test(release))) {
    return res.status(400).json({ error: { code: 'INVALID_RELEASE', message: '发布版本无效' } });
  }
  const suffix = release ? '?release_id=' + encodeURIComponent(release) : '';
  try {
    const response = await backendRequest(req, '/v1/' + area + '/' + parts.map(encodeURIComponent).join('/') + suffix,
      method === 'POST' ? req.body : undefined);
    return res.status(response.status).json(response.data);
  } catch {
    return res.status(503).json({ error: { code: 'BACKEND_UNAVAILABLE', message: '本体服务暂时不可用' } });
  }
}

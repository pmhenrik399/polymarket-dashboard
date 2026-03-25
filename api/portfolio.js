import { readFileSync } from 'fs';
import { join } from 'path';

export default function handler(req, res) {
  try {
    const data = readFileSync(join(process.cwd(), 'data', 'portfolio.json'), 'utf8');
    res.setHeader('Content-Type', 'application/json');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.status(200).send(data);
  } catch (err) {
    res.status(500).json({ error: 'Could not read portfolio data' });
  }
}

import { redirect } from 'next/navigation';
import { requireViewer } from '@/lib/auth';

export default async function Home() {
  await requireViewer();
  redirect('/documents');
}

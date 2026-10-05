import { LoadingPage } from '@/components/LoadingPage';

export default function Loading() {
  return <LoadingPage title="Upload a document" active="/documents" blocks={2} />;
}

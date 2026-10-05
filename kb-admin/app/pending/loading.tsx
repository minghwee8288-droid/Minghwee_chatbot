import { LoadingPage } from '@/components/LoadingPage';

export default function Loading() {
  return <LoadingPage title="Pending approval" active="/pending" blocks={2} />;
}

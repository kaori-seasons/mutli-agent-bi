import { useEffect, useState } from 'react';
import { Amplify } from 'aws-amplify';
import { Authenticator } from '@aws-amplify/ui-react';
import '@aws-amplify/ui-react/styles.css';
import ReviewDesk from '../components/Ontology/ReviewDesk';

export default function OntologyReview() {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    fetch('/api/getConfig').then(async response => {
      if (!response.ok) throw new Error();
      const config = await response.json();
      const userPoolId = process.env.NEXT_PUBLIC_USER_POOL_ID || config.userPoolId;
      const userPoolClientId = process.env.NEXT_PUBLIC_USER_CLIENT_ID || config.userPoolClientId;
      if (!userPoolId || !userPoolClientId) throw new Error();
      Amplify.configure({ Auth: { Cognito: { userPoolId, userPoolClientId } } });
      if (active) setReady(true);
    }).catch(() => { if (active) setError('身份认证配置不可用，请联系管理员。'); });
    return () => { active = false; };
  }, []);
  if (!ready) return <p role={error ? 'alert' : 'status'} className="p-8">{error || '正在加载登录配置…'}</p>;
  return <Authenticator hideSignUp>{({ signOut }) => <ReviewDesk onSignOut={signOut} />}</Authenticator>;
}

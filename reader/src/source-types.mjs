export const SOURCE_TYPES = ['官网', '微信公众号', '其他'];
const INSTITUTIONAL_DOMAINS = ['nju.edu.cn', 'pku.edu.cn', 'neea.edu.cn', 'csc.edu.cn'];
const isDomainOrSubdomain = (host, domain) => host === domain || host.endsWith(`.${domain}`);

export function sourceTypeForHost(host) {
  const hostname = host.toLowerCase().replace(/\.$/, '');
  if (isDomainOrSubdomain(hostname, 'mp.weixin.qq.com')) return '微信公众号';
  if (INSTITUTIONAL_DOMAINS.some((domain) => isDomainOrSubdomain(hostname, domain))
    || isDomainOrSubdomain(hostname, 'edu') || isDomainOrSubdomain(hostname, 'gov.cn')) return '官网';
  return '其他';
}

export function sourceTypesForHosts(hosts) {
  let official = false, wechat = false;
  for (const host of hosts) {
    const type = sourceTypeForHost(host);
    if (type === '官网') official = true;
    if (type === '微信公众号') wechat = true;
  }
  const types = [];
  if (official) types.push('官网');
  if (wechat) types.push('微信公众号');
  if (!types.length && hosts.length) types.push('其他');
  return types;
}

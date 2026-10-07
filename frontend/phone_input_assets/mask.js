export default function({parentElement, data, setStateValue}) {
  const input = parentElement.querySelector('input');
  const error = parentElement.querySelector('#phone-error');
  const digits = text => text.replace(/[^0-9]/g, '');
  const format = number => {
    const groups = [number.slice(0,3),number.slice(3,5),number.slice(5,8),number.slice(8,10),number.slice(10,12)];
    return number ? '+' + groups.filter(Boolean).map((g,i)=>i===0?g:(i<3?' ':'-')+g).join('') : '';
  };
  let last = data?.value ? format(digits(data.value)) : (data?.edited ? '' : '+375');
  if(input.value !== last) input.value = last;
  error.textContent = data?.error || '';
  input.setAttribute('aria-invalid', String(!!data?.error));
  const publish = message => {
    error.textContent = message;
    input.setAttribute('aria-invalid', String(!!message));
    setStateValue('phone', {value: input.value === '+375' ? '' : input.value, error:message, edited:true});
  };
  const before = event => {
    if (event.inputType === 'insertText' && event.data && !/^[0-9+ ()\-]+$/.test(event.data)) {
      event.preventDefault(); publish('Допускаются только цифры.'); return;
    }
    if (event.inputType?.startsWith('delete') && input.selectionStart === input.selectionEnd) {
      let pos = input.selectionStart;
      const backward = event.inputType === 'deleteContentBackward';
      let target = backward ? pos-1 : pos;
      while(target>=0 && target<input.value.length && !/[0-9]/.test(input.value[target])) target += backward ? -1 : 1;
      if(target>=0 && target<input.value.length) input.setSelectionRange(target,target+1);
    }
  };
  const change = () => {
    const raw = input.value;
    const count = digits(raw.slice(0,input.selectionStart)).length;
    if (!/^[0-9+ ()\-]*$/.test(raw) || (raw.match(/\+/g)||[]).length>1 || (raw.includes('+') && !raw.startsWith('+')) || digits(raw).length>12) {
      input.value = last;
      publish(digits(raw).length>12 ? 'Слишком много цифр: нужен код страны из 3 цифр и номер из 9 цифр.' : 'Допускаются только цифры.');
      return;
    }
    input.value = format(digits(raw));
    let cursor=0, seen=0;
    while(cursor<input.value.length && seen<count) {if(/[0-9]/.test(input.value[cursor])) seen++; cursor++;}
    input.setSelectionRange(cursor,cursor);
    last = input.value;
    publish('');
  };
  input.addEventListener('beforeinput',before);
  input.addEventListener('input',change);
  return () => {input.removeEventListener('beforeinput',before); input.removeEventListener('input',change);};
}

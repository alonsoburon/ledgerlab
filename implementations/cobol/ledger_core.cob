       >>SOURCE FORMAT FREE
identification division.
program-id. ledger_authorize.
data division.
linkage section.
01 source-balance pic s9(18) comp-5.
01 transfer-amount pic s9(18) comp-5.
01 allowed pic s9(9) comp-5.
01 debit-entry pic s9(18) comp-5.
01 credit-entry pic s9(18) comp-5.
procedure division using source-balance transfer-amount allowed debit-entry credit-entry.
    move 0 to allowed
    move 0 to debit-entry
    move 0 to credit-entry
    if transfer-amount > 0 and source-balance >= transfer-amount
        move 1 to allowed
        compute debit-entry = 0 - transfer-amount
        move transfer-amount to credit-entry
    end-if
    goback.
end program ledger_authorize.

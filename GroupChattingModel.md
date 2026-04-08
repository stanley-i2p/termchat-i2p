## Group Chatting quick overview

Although **group chatting** is **not considered secure** from an **OpSec** perspective—and this feature was **deliberately left out** of the **Termchat-I2P maximum security tool**—its implementation is pretty **straightforward** for the current architectural model.

### Brief architecture 

1. keep one isolated 1:1 session per peer
2. add a local room layer on top that fans one typed message out to multiple separate peers
3. each recipient gets its own encrypted copy
4. each peer keeps its own:
    * connection
    * TOFU state
    * offline secret
    * deaddrop indexes
    * delivery state

**So architecturally we have:**

* UI group
* multiple independent underlying 1:1 channels

That preserves **compartmentalization much better than a real shared group session**.

In this proposed model, your local client is the coordinator:

* you type once into a **group**
* your client sends separate 1:1 messages to each member
* each recipient just sees a normal message from you, optionally tagged with room/group name

So the **group** exists only as:

* local membership list
* local UI abstraction
* multiple parallel direct sends

(No server host, no room host, no shared group daemon.)

**For a full mesh group of N members:**

Let's assume that each member has N - 1 direct 1:1 relationships (as per our current max. security model) :)))

**Total simultaneous connections:**
$$ \frac{N(N-1)}{2} $$


### Usability and Security

Theoretically (having tested it a bit, but **not extensively**), group chat can feel like an **ordinary group chat** at the UX level.

This approach is much more **compartmentalized ** than a **true shared group session**.

* each peer link is separate
* compromise or reset of one relationship does not automatically break all others
* per-peer TOFU, offline state, and delivery state stay isolated
* there is no single shared room secret whose exposure collapses the whole group

### So, I tend to think that this model is much better security-wise than just a regular shared group chat; besides, it fits pretty well with my initial maximum-security model.



import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react"
import toast from "react-hot-toast"
import api from "../api/axios"
import { useChats } from "./ChatsContext"

const CallContext = createContext()

// STUN alone only helps two peers find each other for a *direct* connection —
// it does nothing when a direct connection isn't possible (strict/symmetric
// NAT, restrictive firewalls). TURN credentials are fetched fresh from the
// Django backend before each call so they are time-limited and never baked
// into the frontend bundle (see backend/apps/users/turn_credentials.py).
const STUN_ONLY = { iceServers: [{ urls: "stun:stun.l.google.com:19302" }] }

async function fetchIceConfig() {
  try {
    const { data } = await api.get("/turn-credentials/")
    return {
      iceServers: [
        { urls: "stun:stun.l.google.com:19302" },
        { urls: data.urls, username: data.username, credential: data.credential },
      ],
    }
  } catch (err) {
    // Fall back to STUN-only — TURN relay is unavailable but direct calls may still work.
    // Log status/message only; never log the credential or username.
    console.error("TURN credential fetch failed — using STUN only:", err?.response?.status ?? err?.message)
    return STUN_ONLY
  }
}

export function CallProvider({ children, currentUser, sendCallSignal, subscribeCallSignals }) {
  // idle | outgoing | ringing | connected
  const [callState, setCallState] = useState("idle")
  const [callType, setCallType] = useState("audio")
  const [remoteUser, setRemoteUser] = useState(null)
  const [isMuted, setIsMuted] = useState(false)
  const [isVideoOn, setIsVideoOn] = useState(false)
  const [localStream, setLocalStream] = useState(null)
  const [remoteStream, setRemoteStream] = useState(null)

  const peerConnectionRef = useRef(null)
  const pendingOfferRef = useRef(null)
  const pendingCandidatesRef = useRef([])
  const pendingConversationIdRef = useRef(null)
  const callStartTimeRef = useRef(null)

  // Only the caller has pendingConversationIdRef set (from openCallPrompt),
  // so only the caller ever logs a call-history message — avoids both sides
  // creating duplicate entries for the same call.
  const logCallOutcome = useCallback((status) => {
    const conversationId = pendingConversationIdRef.current
    if (!conversationId) return

    const duration = callStartTimeRef.current
      ? Math.round((Date.now() - callStartTimeRef.current) / 1000)
      : null

    api.post("/log-call/", {
      conversation_id: conversationId,
      call_status: status,
      is_video: callType === "video",
      duration_seconds: duration,
    }).catch((err) => console.error("Log call error:", err))
  }, [callType])

  const cleanup = useCallback(() => {
    peerConnectionRef.current?.close()
    peerConnectionRef.current = null
    pendingOfferRef.current = null
    pendingCandidatesRef.current = []
    pendingConversationIdRef.current = null
    callStartTimeRef.current = null

    localStream?.getTracks().forEach((t) => t.stop())

    setLocalStream(null)
    setRemoteStream(null)
    setRemoteUser(null)
    setIsMuted(false)
    setIsVideoOn(false)
    setCallState("idle")
  }, [localStream])

  const createPeerConnection = useCallback((targetUserId, iceConfig) => {
    const pc = new RTCPeerConnection(iceConfig)

    pc.onicecandidate = (e) => {
      if (e.candidate) {
        sendCallSignal({
          action: "call_ice_candidate",
          target_user_id: targetUserId,
          candidate: e.candidate,
        })
      }
    }

    pc.ontrack = (e) => {
      setRemoteStream(e.streams[0])
    }

    pc.onconnectionstatechange = () => {
      if (["failed", "disconnected", "closed"].includes(pc.connectionState)) {
        // Remote side dropped without a clean call_end signal.
      }
    }

    peerConnectionRef.current = pc
    return pc
  }, [sendCallSignal])

  const openCallPrompt = useCallback((targetUser, conversationId) => {
    if (callState !== "idle") return
    pendingConversationIdRef.current = conversationId
    setRemoteUser(targetUser)
    setCallState("prompt")
  }, [callState])

  const closeCallPrompt = useCallback(() => {
    setRemoteUser(null)
    pendingConversationIdRef.current = null
    setCallState("idle")
  }, [])

  const startCall = useCallback(async (type) => {
    if (callState !== "prompt" || !remoteUser) return
    const targetUser = remoteUser
    const conversationId = pendingConversationIdRef.current

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: true,
        video: type === "video",
      })
      setLocalStream(stream)
      setCallType(type)
      setIsVideoOn(type === "video")
      setRemoteUser(targetUser)
      setCallState("outgoing")

      const iceConfig = await fetchIceConfig()
      const pc = createPeerConnection(targetUser.id, iceConfig)
      stream.getTracks().forEach((track) => pc.addTrack(track, stream))

      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)

      sendCallSignal({
        action: "call_offer",
        target_user_id: targetUser.id,
        conversation_id: conversationId,
        call_type: type,
        sdp: offer,
      })
    } catch (err) {
      console.error("Start call error:", err)
      toast.error("Could not access camera/microphone")
      cleanup()
    }
  }, [callState, remoteUser, createPeerConnection, sendCallSignal, cleanup])

  const acceptCall = useCallback(async () => {
    const offerData = pendingOfferRef.current
    if (!offerData) return

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: true,
        video: offerData.call_type === "video",
      })
      setLocalStream(stream)
      setIsVideoOn(offerData.call_type === "video")

      const iceConfig = await fetchIceConfig()
      const pc = createPeerConnection(offerData.from_user_id, iceConfig)
      stream.getTracks().forEach((track) => pc.addTrack(track, stream))

      await pc.setRemoteDescription(new RTCSessionDescription(offerData.sdp))

      for (const candidate of pendingCandidatesRef.current) {
        await pc.addIceCandidate(new RTCIceCandidate(candidate))
      }
      pendingCandidatesRef.current = []

      const answer = await pc.createAnswer()
      await pc.setLocalDescription(answer)

      sendCallSignal({
        action: "call_answer",
        target_user_id: offerData.from_user_id,
        sdp: answer,
      })

      callStartTimeRef.current = Date.now()
      setCallState("connected")
    } catch (err) {
      console.error("Accept call error:", err)
      toast.error("Could not access camera/microphone")
      sendCallSignal({ action: "call_reject", target_user_id: offerData.from_user_id })
      cleanup()
    }
  }, [createPeerConnection, sendCallSignal, cleanup])

  const rejectCall = useCallback(() => {
    const offerData = pendingOfferRef.current
    if (offerData) {
      sendCallSignal({ action: "call_reject", target_user_id: offerData.from_user_id })
    }
    cleanup()
  }, [sendCallSignal, cleanup])

  const endCall = useCallback(() => {
    if (remoteUser) {
      sendCallSignal({ action: "call_end", target_user_id: remoteUser.id })
    }
    logCallOutcome(callStartTimeRef.current ? "completed" : "missed_or_canceled")
    cleanup()
  }, [remoteUser, sendCallSignal, logCallOutcome, cleanup])

  const toggleMute = useCallback(() => {
    if (!localStream) return
    const next = !isMuted
    localStream.getAudioTracks().forEach((t) => (t.enabled = !next))
    setIsMuted(next)
  }, [localStream, isMuted])

  const toggleVideo = useCallback(() => {
    if (!localStream || callType !== "video") return
    const next = !isVideoOn
    localStream.getVideoTracks().forEach((t) => (t.enabled = next))
    setIsVideoOn(next)
  }, [localStream, isVideoOn, callType])

  const handleCallSignal = useCallback(async (data) => {
    if (data.type === "incoming_call") {
      if (callState !== "idle") {
        // Already busy — tell the caller instead of silently ignoring.
        sendCallSignal({ action: "call_busy", target_user_id: data.from_user_id })
        return
      }
      pendingOfferRef.current = data
      setRemoteUser({ id: data.from_user_id, username: data.from_display_name })
      setCallType(data.call_type || "audio")
      setCallState("ringing")
      return
    }

    if (data.type === "call_answered") {
      const pc = peerConnectionRef.current
      if (!pc) return
      await pc.setRemoteDescription(new RTCSessionDescription(data.sdp))
      for (const candidate of pendingCandidatesRef.current) {
        await pc.addIceCandidate(new RTCIceCandidate(candidate))
      }
      pendingCandidatesRef.current = []
      callStartTimeRef.current = Date.now()
      setCallState("connected")
      return
    }

    if (data.type === "call_ice_candidate") {
      const pc = peerConnectionRef.current
      if (pc && pc.remoteDescription) {
        try {
          await pc.addIceCandidate(new RTCIceCandidate(data.candidate))
        } catch (err) {
          console.error("ICE candidate error:", err)
        }
      } else {
        pendingCandidatesRef.current.push(data.candidate)
      }
      return
    }

    if (data.type === "call_unavailable") {
      toast("This user doesn't accept calls", { icon: "📵" })
      logCallOutcome("missed_or_canceled")
      cleanup()
      return
    }

    if (data.type === "call_ended") {
      if (data.reason === "call_reject") {
        toast("Call declined", { icon: "📵" })
        logCallOutcome("declined")
      } else if (data.reason === "call_busy") {
        toast("User is busy", { icon: "📵" })
        logCallOutcome("missed_or_canceled")
      } else {
        logCallOutcome(callStartTimeRef.current ? "completed" : "missed_or_canceled")
      }
      cleanup()
    }
  }, [callState, cleanup, sendCallSignal, logCallOutcome])

  useEffect(() => {
    if (!subscribeCallSignals) return
    return subscribeCallSignals(handleCallSignal)
  }, [subscribeCallSignals, handleCallSignal])

  return (
    <CallContext.Provider
      value={{
        callState,
        callType,
        remoteUser,
        isMuted,
        isVideoOn,
        localStream,
        remoteStream,
        openCallPrompt,
        closeCallPrompt,
        startCall,
        acceptCall,
        rejectCall,
        endCall,
        toggleMute,
        toggleVideo,
        handleCallSignal,
      }}
    >
      {children}
    </CallContext.Provider>
  )
}

export const useCall = () => useContext(CallContext)

// Bridges CallProvider to the ChatsContext socket (sendCallSignal/
// subscribeCallSignals) — must be rendered inside a <ChatsProvider>.
export function ConnectedCallProvider({ children, currentUser }) {
  const { sendCallSignal, subscribeCallSignals } = useChats()
  return (
    <CallProvider
      currentUser={currentUser}
      sendCallSignal={sendCallSignal}
      subscribeCallSignals={subscribeCallSignals}
    >
      {children}
    </CallProvider>
  )
}
